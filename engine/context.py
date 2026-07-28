"""TROU 1 — ce qui entre dans le contexte VIERGE du sous-agent.

`build_context` est une **fonction pure** : ni disque, ni reseau, ni horloge.
Elle prend un `Plan` et une `Task`, elle rend une chaine. Elle est donc
testable isolement, et c'est le seul endroit ou se decide ce qu'un sous-agent
a le droit de savoir.

Par defaut elle assemble, et RIEN d'autre :

1. la question globale (une phrase) ;
2. les watch_outs ;
3. pour chaque tache listee dans `depends_on`, uniquement son `insight`.

Ce qui n'y entre JAMAIS : le logbook complet, les `summary`, les artefacts,
le run_log, les autres taches, les resultats bruts d'outils.
"""

from __future__ import annotations

from engine.config import ContextConfig
from engine.schemas import Plan, Task

_DEFAULT = ContextConfig()


def first_sentence(text: str, limit: int = 300) -> str:
    """Reduit un texte a sa premiere phrase (la question globale tient en une)."""
    text = " ".join((text or "").split())
    for sep in (". ", " ? ", " ! "):
        head, found, _ = text.partition(sep)
        if found:
            return (head + found).strip()[:limit]
    if text.endswith(("?", ".", "!")):
        return text[:limit]
    return text[:limit]


def build_context(plan: Plan, task: Task, cfg: ContextConfig | None = None) -> str:
    cfg = cfg or _DEFAULT
    blocks: list[str] = []

    if cfg.include_question:
        question = first_sentence(plan.brief.question)
        if question:
            blocks.append(f"## Question globale de l'investigation\n{question}")

    if cfg.include_watch_outs and plan.brief.watch_outs:
        points = "\n".join(f"- {w}" for w in plan.brief.watch_outs)
        blocks.append(f"## Points de vigilance\n{points}")

    if cfg.include_dependencies and task.depends_on:
        # On ne garde que les N dependances les plus recentes, dans l'ordre du plan.
        kept = task.depends_on[-cfg.max_dependencies :] if cfg.max_dependencies else []
        lignes = []
        for dep_id in kept:
            finding = plan.finding(dep_id)
            insight = first_sentence(finding.insight) if finding else ""
            lignes.append(f"- `{dep_id}` : {insight or '(aucun insight disponible)'}")
        if lignes:
            blocks.append(
                "## Acquis des taches dont celle-ci depend\n"
                "(un insight par tache, rien de plus — ne les re-verifie pas)\n"
                + "\n".join(lignes)
            )

    text = "\n\n".join(blocks).strip()
    if cfg.max_chars and len(text) > cfg.max_chars:
        text = text[: cfg.max_chars].rstrip() + "\n[... contexte tronque]"
    return text


def render_task(task: Task) -> str:
    """La tache elle-meme, formulee pour un contexte vierge."""
    lines = [
        "## Ta tache (la seule)",
        f"**id** : {task.id}",
        f"**titre** : {task.title}",
        f"**question a laquelle tu dois repondre** : {task.question}",
    ]
    if task.scope:
        lines.append(f"**perimetre autorise** : {task.scope}")
    if task.expected_shape:
        lines.append(f"**forme attendue de la reponse** : {task.expected_shape}")
    if task.sanity_check:
        lines.append(f"**verification de coherence a faire** : {task.sanity_check}")
    return "\n".join(lines)
