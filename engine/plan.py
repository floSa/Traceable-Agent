"""Lecture / ecriture du fichier `plan.md` — la seule memoire du systeme.

Format retenu
-------------
`plan.md` = frontmatter YAML + corps markdown.

- Le **frontmatter est la source de verite** : il porte l'integralite de
  l'etat (meta a plat, puis `brief:` et `logbook:`). C'est lui, et lui
  seul, qui est relu.
- Le **corps markdown est une vue generee** (Jinja2, `templates/plan.md.j2`)
  destinee a l'humain. Il est reecrit a chaque commit et n'est jamais parse.

Consequence : le round-trip est sans perte, aucun LLM n'a besoin d'ecrire
du markdown, et un humain peut editer le YAML a la main entre deux runs
(`inv tasks`), la revalidation complete se chargeant de refuser les
editions incoherentes.

Toute ecriture est atomique (fichier temporaire + `os.replace`) et prise
sous verrou exclusif `flock`, pour qu'un kill -9 au milieu de la boucle
laisse toujours un fichier valide sur disque.
"""

from __future__ import annotations

import fcntl
import io
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import frontmatter
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from ruamel.yaml import YAML

from engine.schemas import Adjustment, Plan, RunLogEntry, TaskResult, utcnow

TEMPLATES_DIR = Path(__file__).parent / "templates"

_yaml = YAML(typ="rt")
_yaml.default_flow_style = False
_yaml.width = 100000  # jamais de retour a la ligne automatique
_yaml.allow_unicode = True


class _RuamelHandler(frontmatter.YAMLHandler):
    """Branche ruamel.yaml dans python-frontmatter."""

    def load(self, fm: str, **kwargs: Any) -> dict[str, Any]:
        return _yaml.load(fm) or {}

    def export(self, metadata: dict[str, Any], **kwargs: Any) -> str:
        buf = io.StringIO()
        _yaml.dump(metadata, buf)
        return buf.getvalue().strip()


_HANDLER = _RuamelHandler()


def jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["ts"] = lambda v: v.strftime("%Y-%m-%d %H:%M:%SZ") if isinstance(v, datetime) else str(v)
    return env


# --------------------------------------------------------------------------


class PlanStore:
    """Acces disque a une investigation."""

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.plan_path = self.dir / "plan.md"
        self.artifacts_dir = self.dir / "artifacts"
        self.results_dir = self.dir / "results"
        self.lock_path = self.dir / ".plan.lock"

    # ---- creation --------------------------------------------------------

    @classmethod
    def create(cls, root: Path | str, plan: Plan) -> PlanStore:
        root = Path(root)
        directory = root / f"{plan.created_at.date().isoformat()}_{plan.slug}"
        store = cls(directory)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.artifacts_dir.mkdir(exist_ok=True)
        store.results_dir.mkdir(exist_ok=True)
        store.save(plan)
        return store

    # ---- verrou ----------------------------------------------------------

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ---- lecture / ecriture ---------------------------------------------

    def exists(self) -> bool:
        return self.plan_path.is_file()

    def load(self) -> Plan:
        """Relit le plan depuis le disque et le revalide integralement."""
        post = frontmatter.load(str(self.plan_path), handler=_HANDLER)
        return Plan.model_validate(dict(post.metadata))

    def save(self, plan: Plan) -> None:
        """Ecriture atomique. Revalide avant d'ecrire : on ne pose jamais
        sur disque un etat qui ne se relirait pas."""
        plan = Plan.model_validate(plan.model_dump(mode="python"))
        meta = plan.model_dump(mode="json")
        body = jinja_env().get_template("plan.md.j2").render(plan=plan)
        post = frontmatter.Post(body, handler=_HANDLER, **meta)
        text = frontmatter.dumps(post, handler=_HANDLER) + "\n"

        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".plan.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.plan_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        # durabilite du rename lui-meme
        dir_fd = os.open(str(self.dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self.sweep_orphan_tmp()

    def sweep_orphan_tmp(self, older_than_s: float = 60.0) -> list[Path]:
        """Un kill -9 pile entre `mkstemp` et `os.replace` laisse un temporaire
        derriere lui. Il n'a jamais pu corrompre `plan.md` (le rename est
        atomique), mais il encombre : on balaie les vieux orphelins.

        Le seuil d'age evite de supprimer l'ecriture en vol d'un autre process :
        une ecriture prend des millisecondes, un orphelin vit indefiniment.
        """
        supprimes = []
        limite = time.time() - older_than_s
        for tmp in self.dir.glob(".plan.*.tmp"):
            try:
                if tmp.stat().st_mtime < limite:
                    tmp.unlink()
                    supprimes.append(tmp)
            except OSError:  # deja disparu, ou pas a nous
                continue
        return supprimes

    # ---- mutation --------------------------------------------------------

    def mutate(self, fn: Callable[[Plan], None]) -> Plan:
        """Relit le fichier, applique `fn`, revalide, reecrit. Sous verrou."""
        with self.lock():
            plan = self.load()
            fn(plan)
            self.save(plan)
            return plan

    def commit(self, result: TaskResult, run_entry: RunLogEntry | None = None) -> Plan:
        """Ecrit le resultat d'une tache : etat de la tache + finding (+ run_log).

        Idempotent sur le finding (remplace celui de la meme tache), ce qui
        rend un `--retry-blocked` propre.

        `run_entry` est ecrit dans la MEME transaction que le finding : sinon un
        kill entre les deux laisse une tache `done` sans ligne de run_log, donc
        des tokens depenses et non comptes.
        """

        def apply(plan: Plan) -> None:
            task = plan.task(result.task_id)
            if task is None:
                raise KeyError(f"tache inconnue: {result.task_id}")
            task.state = result.status
            finding = result.to_finding()
            if result.status == "blocked" and result.reason:
                finding.summary = finding.summary or f"BLOCKED: {result.reason}"
            existing = next(
                (i for i, f in enumerate(plan.logbook.findings) if f.task_id == result.task_id),
                None,
            )
            if existing is None:
                plan.logbook.findings.append(finding)
            else:
                plan.logbook.findings[existing] = finding
            if run_entry is not None:
                plan.logbook.run_log.append(run_entry)
            counts = plan.counts()
            plan.logbook.progress.append(
                f"{result.task_id} -> {result.status} "
                f"({counts['done']}/{len(plan.brief.tasks)} done)"
            )

        return self.mutate(apply)

    @staticmethod
    def run_entry(
        task_id: str, status: str, started: datetime, duration_s: float, tokens: int
    ) -> RunLogEntry:
        return RunLogEntry(
            task_id=task_id,
            status=status,  # type: ignore[arg-type]
            started_utc=started,
            duration_s=round(duration_s, 3),
            tokens=tokens,
        )

    def backfill_run_log(
        self,
        task_id: str,
        status: str,
        started: datetime,
        duration_s: float,
        tokens: int,
    ) -> Plan:
        """Ajoute une ligne de run_log seule (consommations hors tache :
        evaluation du critere d'arret, synthese)."""
        entry = self.run_entry(task_id, status, started, duration_s, tokens)
        return self.mutate(lambda p: p.logbook.run_log.append(entry))

    def set_status(self, status: str) -> Plan:
        def apply(plan: Plan) -> None:
            plan.status = status  # type: ignore[assignment]

        return self.mutate(apply)

    def add_adjustment(self, step: str, change: str, reason: str) -> Plan:
        adj = Adjustment(step=step, change=change, reason=reason)
        return self.mutate(lambda p: p.logbook.adjustments.append(adj))

    # ---- artefacts (write-only, jamais relus par un LLM) -----------------

    def write_artifact(self, task_id: str, index: int, tool: str, content: str) -> str:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        name = f"{task_id}_{index:02d}_{tool}.txt"
        path = self.artifacts_dir / name
        header = f"# {utcnow().isoformat()} | task={task_id} | tool={tool}\n"
        path.write_text(header + content.rstrip() + "\n", encoding="utf-8")
        return f"artifacts/{name}"


def find_investigation(root: Path | str, slug: str) -> PlanStore:
    """Retrouve une investigation par slug (le dossier est prefixe par la date)."""
    root = Path(root)
    candidates = sorted(p for p in root.glob(f"*_{slug}") if (p / "plan.md").is_file())
    if not candidates:
        raise FileNotFoundError(f"aucune investigation pour le slug {slug!r} dans {root}")
    return PlanStore(candidates[-1])
