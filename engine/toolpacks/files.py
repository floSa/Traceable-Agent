"""`FilesToolPack` — investigation sur un dossier de fichiers texte.

Pack par defaut du POC. Aucune dependance hors stdlib.

Outils : `grep(pattern, glob)`, `read(path, start, end)`, `list(glob)`.

Perimetre (`task.scope`) :

    scope: {"globs": ["notes/**/*.md", "rapports/*.txt"]}

Tout chemin hors de ces globs est refuse par `validate`, donc jamais lu :
c'est l'equivalent, cote fichiers, des tables declarees du pack sql.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from engine.config import Config
from engine.toolpacks.base import BasePack, Ok, Refus, ToolResult, ToolSpec, Verdict, matches_any

DEFAULT_GLOBS = ["**/*"]


class FilesScope(BaseModel):
    """Le perimetre d'une tache, cote fichiers. Schema vu par le brief_builder."""

    globs: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Motifs des fichiers que cette tache a le droit de lire, relatifs a "
            "la racine du corpus. Exemples : 'notes/**/*.md', 'tickets/*.md'. "
            "Jamais de chemin absolu, jamais de '..', jamais le nom du dossier racine."
        ),
    )


class FilesToolPack(BasePack):
    name = "files"

    def __init__(self, cfg: Config, scope: dict[str, Any] | None = None) -> None:
        pack_cfg = cfg.toolpack.files
        super().__init__(scope, max_bytes=pack_cfg.max_bytes)
        self.cfg = pack_cfg
        self.root = cfg.path(pack_cfg.root)
        self.globs: list[str] = list(self.scope.get("globs") or DEFAULT_GLOBS)

    # ---- description injectee au prompt ---------------------------------

    def describe(self) -> str:
        return (
            "Tu investigues un corpus de fichiers texte, en lecture seule.\n"
            f"Fichiers autorises pour CETTE tache : {', '.join(f'`{g}`' for g in self.globs)}\n"
            "Tout ce qui sort de ces motifs est refuse avant execution.\n\n"
            "Les chemins sont relatifs a la racine du corpus : n'ecris JAMAIS "
            f"`{self.root.name}/...` en tete d'un chemin ou d'un glob.\n"
            "Le glob que tu passes est INTERSECTE avec ton perimetre : un seul "
            "`grep(motif, \"**/*\")` couvre donc TOUS tes fichiers d'un coup. "
            "N'appelle pas l'outil fichier par fichier, tu epuiserais ton budget.\n"
            f"{self.sample()}\n\n"
            "Outils :\n" + self.render_tools()
        )

    def sample(self, limite: int = 12) -> str:
        """Un echantillon des fichiers reellement presents.

        Un sous-agent a contexte vierge ne peut pas deviner l'arborescence :
        sans cet ancrage il invente des chemins, obtient zero resultat, et
        brule son budget a reessayer la meme chose.
        """
        try:
            fichiers = self._candidates("**/*")
        except OSError:
            return "Contenu du corpus : indisponible."
        if not fichiers:
            return "Contenu du corpus : aucun fichier dans le perimetre de cette tache."
        noms = [p.relative_to(self.root).as_posix() for p in fichiers[:limite]]
        reste = f"\n  ... et {len(fichiers) - limite} autres" if len(fichiers) > limite else ""
        return "Fichiers reellement presents dans ton perimetre :\n  " + "\n  ".join(noms) + reste

    def scope_hint(self) -> str:
        """Le format de `scope`, illustre avec l'arborescence REELLE."""
        dossiers = sorted({p.parent.as_posix() for p in self._relatifs()} - {"."})
        extensions = sorted({p.suffix for p in self._relatifs() if p.suffix})
        exemples = [f"{d}/**/*{extensions[0] if extensions else ''}" for d in dossiers[:3]]
        return (
            '{"globs": [...]} — motifs des fichiers que la tache a le droit de lire, '
            "relatifs a la racine du corpus (jamais de chemin absolu, jamais de `..`).\n"
            f"  dossiers existants : {', '.join(dossiers) or '(racine seule)'}\n"
            f"  extensions presentes : {', '.join(extensions) or '(aucune)'}\n"
            f"  exemple valide : {exemples or ['**/*']}"
        )

    def scope_model(self) -> type[BaseModel]:
        return FilesScope

    def _relatifs(self) -> list[Path]:
        try:
            return [p.relative_to(self.root) for p in self._candidates("**/*")]
        except OSError:
            return []

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list",
                description="liste les fichiers correspondant a un motif glob",
                args_schema={
                    "type": "object",
                    "properties": {"glob": {"type": "string", "description": "ex: **/*.md"}},
                    "required": ["glob"],
                },
            ),
            ToolSpec(
                name="grep",
                description="cherche une expression reguliere dans les fichiers d'un glob, "
                "renvoie les lignes trouvees avec leur numero",
                args_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "expression reguliere"},
                        "glob": {"type": "string", "description": "ex: **/*.md"},
                    },
                    "required": ["pattern", "glob"],
                },
            ),
            ToolSpec(
                name="read",
                description="lit un fichier, eventuellement entre deux numeros de ligne",
                args_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "chemin relatif a la racine"},
                        "start": {"type": "integer", "description": "1re ligne (defaut 1)"},
                        "end": {"type": "integer", "description": "derniere ligne (defaut fin)"},
                    },
                    "required": ["path"],
                },
            ),
        ]

    # ---- garde-fou : appele AVANT toute execution ------------------------

    def validate(self, name: str, args: dict[str, Any]) -> Verdict:
        refus = self.check_known(name) or self.check_required(name, args)
        if refus:
            return refus

        if name in ("list", "grep"):
            glob = str(args["glob"])
            if bad := self._bad_path(glob):
                return Refus(bad)
            # Un glob demande hors perimetre ne renverra rien : on le dit tout
            # de suite plutot que de laisser l'agent croire que le corpus est vide.
            if not self._glob_intersects_scope(glob):
                # Un refus qui ne dit pas quoi faire fait tourner l'agent en
                # rond : on lui donne le motif qui marche, pas juste le motif
                # qui ne marche pas. (Constate en reel : il tentait de
                # concatener ses fichiers autorises dans un seul glob.)
                return Refus(
                    f"le motif {glob!r} ne recoupe aucun motif autorise pour cette tache.\n"
                    f"Un seul motif par appel, pas de liste ni d'accolades. "
                    f"Utilise `**/*` : il est intersecte avec ton perimetre et couvre "
                    f"donc d'un coup tous tes fichiers autorises ({self.globs})."
                )

        if name == "grep":
            try:
                re.compile(str(args["pattern"]))
            except re.error as exc:
                return Refus(f"expression reguliere invalide: {exc}")

        if name == "read":
            path = str(args["path"])
            if bad := self._bad_path(path):
                return Refus(bad)
            if not matches_any(path, self.globs):
                return Refus(
                    f"chemin hors perimetre: {path!r} n'est couvert par aucun motif "
                    f"autorise pour cette tache ({self.globs})"
                )
            start, end = args.get("start"), args.get("end")
            if start is not None and end is not None and int(end) < int(start):
                return Refus(f"plage de lignes invalide: start={start} > end={end}")

        return Ok()

    @staticmethod
    def _bad_path(value: str) -> str | None:
        if value.startswith("/") or value.startswith("~"):
            return f"chemin absolu interdit: {value!r} (utilise un chemin relatif a la racine)"
        if ".." in Path(value).parts:
            return f"remontee de repertoire interdite: {value!r}"
        return None

    def _glob_intersects_scope(self, glob: str) -> bool:
        """Vrai si le glob demande peut produire au moins un chemin autorise.
        Test purement lexical : `validate` ne touche pas au disque."""
        if any(g in ("**/*", "**") for g in self.globs):
            return True
        # un glob demande "large" (contenant **) recoupe tout motif de meme racine
        for allowed in self.globs:
            if matches_any(allowed, [glob]) or matches_any(glob, [allowed]):
                return True
            if glob.startswith("**/") and allowed.endswith(glob[3:]):
                return True
            if allowed.startswith("**/") and glob.endswith(allowed[3:]):
                return True
        return False

    # ---- execution -------------------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            if name == "list":
                return self._list(str(args["glob"]))
            if name == "grep":
                return self._grep(str(args["pattern"]), str(args["glob"]))
            if name == "read":
                return self._read(
                    str(args["path"]),
                    int(args["start"]) if args.get("start") else 1,
                    int(args["end"]) if args.get("end") else None,
                )
        except OSError as exc:
            return ToolResult.failure(f"erreur d'acces disque: {exc}")
        return ToolResult.failure(f"outil non implemente: {name}")

    def _candidates(self, glob: str) -> list[Path]:
        """Fichiers du glob demande INTERSECTES avec le perimetre autorise."""
        out = []
        for p in sorted(self.root.glob(glob)):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if not matches_any(rel, self.globs):
                continue
            # defense en profondeur : un symlink ne doit pas sortir de la racine
            real = Path(os.path.realpath(p))
            if not real.is_relative_to(Path(os.path.realpath(self.root))):
                continue
            out.append(p)
        return out

    def _list(self, glob: str) -> ToolResult:
        files = self._candidates(glob)
        shown = files[: self.cfg.max_results]
        lines = [
            f"{p.relative_to(self.root).as_posix()}  ({p.stat().st_size} octets)" for p in shown
        ]
        body = "\n".join(lines) or self._rien_trouve(glob)
        if len(files) > len(shown):
            body += f"\n... {len(files) - len(shown)} fichiers de plus (non affiches)"
        return ToolResult(
            ok=True,
            content=self.truncate(body),
            meta={"files_matched": len(files), "files_shown": len(shown)},
        )

    def _grep(self, pattern: str, glob: str) -> ToolResult:
        rx = re.compile(pattern)
        hits: list[str] = []
        scanned = 0
        total = 0
        for p in self._candidates(glob):
            if p.stat().st_size > self.cfg.max_file_bytes:
                continue
            scanned += 1
            rel = p.relative_to(self.root).as_posix()
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    total += 1
                    if len(hits) < self.cfg.max_results:
                        hits.append(f"{rel}:{lineno}: {line.strip()}")
        body = "\n".join(hits) or (
            f"(aucune correspondance pour ce motif dans {scanned} fichier(s))"
            if scanned
            else self._rien_trouve(glob)
        )
        if total > len(hits):
            body += f"\n... {total - len(hits)} correspondances de plus (non affichees)"
        return ToolResult(
            ok=True,
            content=self.truncate(body),
            meta={"files_scanned": scanned, "matches": total, "matches_shown": len(hits)},
        )

    def _rien_trouve(self, glob: str) -> str:
        """Un `(aucun resultat)` sec fait tourner en rond : on redit ou chercher."""
        return f"(aucun fichier ne correspond a `{glob}`)\n{self.sample()}"

    def _read(self, path: str, start: int, end: int | None) -> ToolResult:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            return ToolResult.failure(f"chemin hors racine: {path!r}")
        if not target.is_file():
            return ToolResult.failure(f"fichier introuvable: {path!r}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start)
        end = len(lines) if end is None else min(end, len(lines))
        chunk = lines[start - 1 : end]
        body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(chunk))
        return ToolResult(
            ok=True,
            content=self.truncate(body or "(plage vide)"),
            meta={"path": path, "start": start, "end": end, "total_lines": len(lines)},
        )

    # ---- trace auditable (write-only) ------------------------------------

    def artifact(self, name: str, args: dict[str, Any], res: ToolResult) -> str | None:
        """La commande shell equivalente : rejouable a la main, verifiable."""
        root = self.root
        if name == "list":
            cmd = f"ls {root}/{args.get('glob')}"
        elif name == "grep":
            cmd = f"grep -rnE {args.get('pattern')!r} --include={args.get('glob')!r} {root}"
        elif name == "read":
            start, end = args.get("start") or 1, args.get("end") or "$"
            cmd = f"sed -n '{start},{end}p' {root}/{args.get('path')}"
        else:
            return None
        status = "OK" if res.ok else f"ERREUR: {res.error}"
        meta = " ".join(f"{k}={v}" for k, v in res.meta.items())
        return f"$ {cmd}\n# {status}\n# {meta}\n"
