"""Role 4 — `synthesize` : un seul appel LLM, sur le logbook relu du disque.

C'est le seul role autorise a voir l'ensemble des findings : il ne fait que
recoller ce que les sous-agents ont trouve, il ne va rien chercher lui-meme.

Le resultat part dans `results/` sous forme de **snapshot date immuable** :
on n'ecrase jamais une synthese precedente. Deux syntheses de la meme
investigation coexistent et se comparent — c'est la seule facon de voir qu'une
relance a change les conclusions.

Prerequis : le point de controle humain n° 2 (`inv review` puis `inv approve`).
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.config import Config
from engine.plan import PlanStore
from engine.providers import BaseProvider
from engine.schemas import Plan, Synthesis, Usage, utcnow

SYSTEM = """You synthesize findings that were produced by other agents. You do
not investigate. You do not invent facts. Every claim must trace back to a
finding you were given.

Rends :
- headline       : la reponse en une phrase
- answer         : la reponse argumentee, 10 lignes maximum
- evidence       : les faits qui la soutiennent, chacun rattache a un id de tache
- caveats        : ce que l'enquete n'a PAS etabli, les taches bloquees, les angles morts
- recommendation : ce qu'il faut faire, au regard du seuil d'action
Si les findings ne suffisent pas a trancher, dis-le dans headline.
"""

APPROVAL_STEP = "approve"


def is_approved(plan: Plan) -> bool:
    """Point de controle humain n° 2 : `inv approve` a bien ete passe."""
    return any(a.step == APPROVAL_STEP for a in plan.logbook.adjustments)


def render_logbook(plan: Plan) -> str:
    """Le logbook, tel qu'il est sur disque. Aucun artefact n'y entre :
    les traces sont write-only, un LLM ne les relit jamais.

    Une tache qui n'est pas `done` ne transmet **que** son etat et son motif
    d'echec — jamais la prose que le modele avait redigee. C'est la regle qui
    donne son sens a la porte de verification : si un chiffre est rejete parce
    qu'il est faux, il ne doit pas revenir par la bande dans le prompt de
    synthese, ou il redeviendrait citable.
    """
    blocs = []
    for task in plan.brief.tasks:
        finding = plan.finding(task.id)
        entete = f"### `{task.id}` — {task.title}\netat: {task.state}\nquestion: {task.question}"

        if finding is None:
            blocs.append(f"{entete}\naucun resultat")
            continue

        if task.state != "done":
            motifs = [o.detail for o in finding.checks if not o.passed]
            raison = " ; ".join(motifs) or finding.summary or "sans motif enregistre"
            blocs.append(
                f"{entete}\nAUCUN RESULTAT EXPLOITABLE — cette tache a ete arretee : {raison}\n"
                "N'invente pas sa reponse : elle fait partie de ce qui n'a PAS ete etabli."
            )
            continue

        blocs.append(
            f"{entete}\n"
            f"resume: {finding.summary}\n"
            f"insight: {finding.insight}\n"
            f"influence: {finding.influence}"
        )
    return "\n\n".join(blocs)


def build_prompt(plan: Plan) -> str:
    frame = plan.brief.decision_frame
    return (
        f"## Question de l'investigation\n{plan.brief.question}\n\n"
        f"## Cadre de decision\n"
        f"- decision a prendre : {frame.decision or '—'}\n"
        f"- seuil d'action : {frame.action_threshold or '—'}\n"
        f"- critere d'arret : {frame.kill_criterion or '—'}\n\n"
        f"## Points de vigilance\n"
        + ("\n".join(f"- {w}" for w in plan.brief.watch_outs) or "(aucun)")
        + f"\n\n## Livrable attendu\n{plan.brief.deliverable or '—'}\n\n"
        f"## Findings (logbook relu du disque)\n{render_logbook(plan)}"
    )


def snapshot_path(store: PlanStore, suffix: str) -> Path:
    """Chemin date, jamais ecrase."""
    store.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
    path = store.results_dir / f"{stamp}_{suffix}"
    n = 2
    while path.exists():
        path = store.results_dir / f"{stamp}-{n}_{suffix}"
        n += 1
    return path


def synthesize(store: PlanStore, cfg: Config, provider: BaseProvider) -> tuple[Synthesis, Path, Usage]:
    plan = store.load()  # relu du disque, comme tout le reste
    if not is_approved(plan):
        raise PermissionError(
            "findings non approuvees : passe par `inv review <slug>` puis `inv approve <slug>` "
            "avant de synthetiser"
        )

    result, usage = provider.complete(SYSTEM, build_prompt(plan), Synthesis, cfg.synthesis.max_tokens)
    assert isinstance(result, Synthesis)

    path = snapshot_path(store, "synthesis.json")
    payload = {
        "slug": plan.slug,
        "title": plan.title,
        "question": plan.brief.question,
        "generated_utc": utcnow().isoformat(),
        "model": cfg.llm.model,
        "tokens": usage.total,
        "tasks": {t.id: t.state for t in plan.brief.tasks},
        "synthesis": result.model_dump(mode="json"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    store.backfill_run_log("synthesize", "done", utcnow(), 0.0, usage.total)
    return result, path, usage


def latest_synthesis(store: PlanStore) -> dict | None:
    fichiers = sorted(store.results_dir.glob("*_synthesis.json"))
    if not fichiers:
        return None
    return json.loads(fichiers[-1].read_text(encoding="utf-8"))
