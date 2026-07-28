"""Role 3 — un sous-agent = une tache, un contexte vierge.

Le sous-agent ne sait rien de l'investigation en dehors de ce que
`build_context` lui donne. Il ne connait ni le logbook, ni les autres taches,
ni ce qu'il a fait la tache d'avant : il n'y a pas de "tache d'avant" pour lui.

Sa boucle d'outils est bornee (TROU 4). Tout depassement — tours, tokens,
provider en echec — se solde par une tache `blocked` portant sa raison,
jamais par une exception qui remonte dans la boucle principale.
"""

from __future__ import annotations

import json
from typing import Any

from engine.config import Config
from engine.context import build_context, render_task
from engine.plan import PlanStore
from engine.providers import BaseProvider, ProviderError
from engine.schemas import AgentStep, Plan, StopEvaluation, Task, TaskResult, Usage
from engine.toolpacks.base import Refus, ToolPack

SYSTEM = """You execute exactly one task. You do not summarize. You do not comment on other tasks.

Tu disposes d'outils en lecture seule. Marche a suivre :
1. appelle un outil (action="call_tool") pour aller chercher des faits ;
2. quand tu as de quoi repondre, rends action="final_answer" avec :
   - summary   : ce que tu as trouve, factuel, avec les references (fichier:ligne)
   - insight   : UNE phrase, la conclusion utile pour la suite de l'enquete
   - influence : en quoi ce resultat pese sur la decision a prendre
Tu ne reponds jamais de memoire : tout ce que tu affirmes vient d'un outil.
Si les outils ne permettent pas de conclure, dis-le dans summary et donne
un insight qui commence par "Indetermine :".
"""


def _render_observations(obs: list[str]) -> str:
    if not obs:
        return "## Observations\n(aucune pour l'instant — commence par appeler un outil)"
    return "## Observations deja obtenues\n" + "\n\n".join(obs)


def run_task(
    plan: Plan,
    task: Task,
    pack: ToolPack,
    provider: BaseProvider,
    cfg: Config,
    store: PlanStore | None = None,
) -> TaskResult:
    """Execute une tache et rend son resultat. N'ecrit rien dans le plan."""
    budget_tokens = min(cfg.agent.max_tokens, plan.budget_tokens_per_task)
    system = f"{SYSTEM}\n\n## Outils a ta disposition\n{pack.describe()}"
    context = build_context(plan, task, cfg.context)

    observations: list[str] = []
    artifacts: list[str] = []
    deja_vus: dict[str, str] = {}  # appels deja effectues -> leur resultat
    usage = Usage()
    tool_calls = 0

    def blocked(reason: str) -> TaskResult:
        return TaskResult(
            task_id=task.id,
            status="blocked",
            summary=f"Tache interrompue: {reason}",
            insight="",
            influence="",
            artifacts=artifacts,
            reason=reason,
            tokens=usage.total,
            tool_calls=tool_calls,
        )

    for turn in range(cfg.agent.max_tool_calls + 1):
        restant = budget_tokens - usage.total
        if restant <= 0:
            return blocked(f"budget de tokens epuise ({budget_tokens} tokens)")
        if turn == cfg.agent.max_tool_calls:
            return blocked(
                f"budget d'outils epuise ({cfg.agent.max_tool_calls} appels) "
                "sans reponse finale"
            )

        user = "\n\n".join(
            filter(
                None,
                [
                    context,
                    render_task(task),
                    _render_observations(observations),
                    f"Tours d'outils restants : {cfg.agent.max_tool_calls - turn}."
                    if turn
                    else None,
                ],
            )
        )

        try:
            step, step_usage = provider.complete(
                system,
                user,
                AgentStep,
                max_tokens=min(cfg.agent.max_tokens_per_call, restant),
            )
        except ProviderError as exc:
            usage = usage + exc.usage  # les tentatives ratees comptent aussi
            return blocked(f"modele indisponible ou reponse inexploitable: {exc}")
        usage = usage + step_usage
        assert isinstance(step, AgentStep)

        if step.action == "final_answer":
            return TaskResult(
                task_id=task.id,
                status="done",
                summary=step.summary or "",
                insight=step.insight or "",
                influence=step.influence or "",
                artifacts=artifacts,
                tokens=usage.total,
                tool_calls=tool_calls,
            )

        # --- appel d'outil : validate AVANT toute execution
        name, args = step.tool or "", dict(step.args or {})
        verdict = pack.validate(name, args)
        if isinstance(verdict, Refus):
            tool_calls += 1
            observations.append(
                f"### tour {tool_calls} — `{name}({args})` REFUSE\n{verdict.reason}"
            )
            continue
        if verdict.args is not None:
            args = verdict.args

        # --- anti-boucle : un petit modele rejoue volontiers a l'identique un
        # appel qui n'a rien donne, jusqu'a epuiser son budget. On ne le laisse
        # pas payer deux fois pour la meme reponse.
        signature = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        if signature in deja_vus:
            tool_calls += 1
            observations.append(
                f"### tour {tool_calls} — `{name}({args})` DEJA FAIT\n"
                f"Cet appel EXACT a deja ete passe et a donne :\n{deja_vus[signature]}\n"
                "Ne le repete pas. Change d'outil ou d'arguments, ou reponds "
                "avec ce que tu as (action='final_answer')."
            )
            continue

        result = pack.call(name, args)
        tool_calls += 1

        trace = pack.artifact(name, args, result)
        if trace and store is not None:
            artifacts.append(store.write_artifact(task.id, tool_calls, name, trace))

        rendu = result.content if result.ok else f"ERREUR: {result.error}"
        deja_vus[signature] = rendu
        observations.append(f"### tour {tool_calls} — `{name}({args})`\n{rendu}")

    return blocked("boucle d'outils terminee sans reponse finale")  # pragma: no cover


def describe_budget(cfg: Config, plan: Plan) -> dict[str, Any]:
    return {
        "max_tool_calls": cfg.agent.max_tool_calls,
        "max_tokens": min(cfg.agent.max_tokens, plan.budget_tokens_per_task),
    }


# --------------------------------------------------------------------------
# TROU 2 — evaluation du critere d'arret, strategie `llm`
#
# Vit ici, et pas dans loop.py, pour que l'orchestrateur reste sans le moindre
# appel de modele. C'est un appel dedie MINIMAL : le critere, les insights
# accumules, rien d'autre. Sortie {fired, reason}.
# --------------------------------------------------------------------------

STOP_SYSTEM = """You evaluate a single stop criterion. You do not investigate.
You do not propose next steps. You answer only whether the criterion is met.

Tu recois un critere d'arret et la liste des insights deja obtenus.
fired=true UNIQUEMENT si les insights etablissent que le critere est atteint.
Dans le doute, fired=false : arreter une enquete a tort coute plus cher que
la poursuivre d'une tache.
"""


def evaluate_stop_llm(
    plan: Plan, provider: BaseProvider, max_tokens: int = 2000
) -> tuple[StopEvaluation, Usage]:
    insights = [
        f"- `{f.task_id}` : {f.insight}" for f in plan.logbook.findings if f.insight.strip()
    ]
    user = (
        f"## Critere d'arret\n{plan.brief.decision_frame.kill_criterion or '(aucun)'}\n\n"
        f"## Insights accumules\n" + ("\n".join(insights) or "(aucun)")
    )
    try:
        evaluation, usage = provider.complete(STOP_SYSTEM, user, StopEvaluation, max_tokens)
    except ProviderError as exc:
        # Un evaluateur en panne n'arrete pas l'enquete : il se declare non concluant.
        return StopEvaluation(fired=False, reason=f"evaluation impossible: {exc}"), exc.usage
    assert isinstance(evaluation, StopEvaluation)
    return evaluation, usage
