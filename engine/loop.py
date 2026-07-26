"""Role 2 — l'orchestrateur. ZERO appel LLM.

Cette boucle ne contient aucun prompt, aucun schema de sortie, aucun client
modele. Elle enchaine, pour chaque tour :

    relire le plan sur disque
      -> premiere tache `pending` dont les depends_on sont `done`
      -> appeler le sous-agent
      -> commit
      -> backfill du run_log
      -> verifier les arrets

Trois arrets : budget total epuise, critere d'arret declenche (TROU 2),
N `blocked` consecutifs (circuit breaker). Tous menent a `awaiting_review` :
c'est un humain, jamais le moteur, qui fait passer une investigation a `done`.

TROU 6 — reprise apres echec : l'etat vit integralement dans `plan.md`, relu
au debut de CHAQUE tour. Un process tue en cours de boucle ne perd que la
tache en vol ; `inv run` relance reprend a la premiere `pending`.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.agent import evaluate_stop_llm, run_task
from engine.checks.verify import apply_gate
from engine.config import Config
from engine.plan import PlanStore
from engine.providers import BaseProvider
from engine.schemas import Plan, StopEvaluation, StopLogEntry, TaskResult, utcnow
from engine.toolpacks.base import build_toolpack

Event = Callable[[str, dict[str, Any]], None]


@dataclass
class LoopOutcome:
    reason: str = "toutes les taches sont traitees"
    tasks_run: int = 0
    tokens: int = 0
    results: list[TaskResult] = field(default_factory=list)
    stopped_early: bool = False


def _noop(kind: str, payload: dict[str, Any]) -> None:
    pass


# --------------------------------------------------------------------------


def load_stop_callable(ref: str) -> Callable[[Plan], Any]:
    """TROU 2, strategie `callable` : "mon_module:ma_fonction"."""
    module_name, _, func_name = ref.partition(":")
    if not module_name or not func_name:
        raise ValueError(f"reference invalide {ref!r}, attendu 'module:fonction'")
    return getattr(importlib.import_module(module_name), func_name)


def _normalise(value: Any) -> StopEvaluation:
    if isinstance(value, StopEvaluation):
        return value
    if isinstance(value, bool):
        return StopEvaluation(fired=value, reason="")
    if isinstance(value, tuple) and len(value) == 2:
        return StopEvaluation(fired=bool(value[0]), reason=str(value[1]))
    if isinstance(value, dict):
        return StopEvaluation.model_validate(value)
    raise TypeError(f"sortie de critere d'arret inexploitable: {value!r}")


def evaluate_stop(
    plan: Plan, cfg: Config, provider: BaseProvider | None, after_task: str
) -> tuple[StopLogEntry, int]:
    """Retourne l'entree a journaliser et les tokens consommes.

    L'evaluation est journalisee quelle que soit son issue — y compris quand
    la strategie est `none`, pour que le plan porte la trace explicite que le
    critere n'a jamais ete evalue.
    """
    strategy = cfg.stop.strategy
    if strategy == "none":
        return (
            StopLogEntry(
                after_task=after_task,
                strategy="none",
                fired=False,
                reason="strategie 'none' : critere d'arret jamais evalue",
            ),
            0,
        )

    if strategy == "callable":
        if not cfg.stop.callable:
            return (
                StopLogEntry(
                    after_task=after_task,
                    strategy="callable",
                    fired=False,
                    reason="aucune fonction configuree (stop.callable est vide)",
                ),
                0,
            )
        try:
            evaluation = _normalise(load_stop_callable(cfg.stop.callable)(plan))
        except Exception as exc:  # un critere casse n'arrete pas l'enquete
            evaluation = StopEvaluation(fired=False, reason=f"critere en erreur: {exc}")
        return (
            StopLogEntry(
                after_task=after_task,
                strategy="callable",
                fired=evaluation.fired,
                reason=evaluation.reason,
            ),
            0,
        )

    # strategy == "llm"
    if provider is None:
        return (
            StopLogEntry(
                after_task=after_task, strategy="llm", fired=False, reason="aucun provider"
            ),
            0,
        )
    evaluation, usage = evaluate_stop_llm(plan, provider, cfg.stop.max_tokens)
    return (
        StopLogEntry(
            after_task=after_task,
            strategy="llm",
            fired=evaluation.fired,
            reason=evaluation.reason,
        ),
        usage.total,
    )


# --------------------------------------------------------------------------


def run(
    store: PlanStore,
    cfg: Config,
    provider: BaseProvider,
    retry_blocked: bool | None = None,
    single_step: bool = False,
    on_event: Event = _noop,
) -> LoopOutcome:
    retry_blocked = cfg.loop.retry_blocked if retry_blocked is None else retry_blocked
    outcome = LoopOutcome()
    consecutive_blocked = 0
    # une tache n'est tentee qu'une fois par run : sans ca, `--retry-blocked`
    # rejoue en boucle la premiere tache qui rebloque
    deja_tentees: set[str] = set()
    stop_none_logged = any(s.strategy == "none" for s in store.load().logbook.stop_log)

    store.set_status("in_progress")

    while True:
        plan = store.load()  # <- la seule source de verite, relue a chaque tour

        if plan.tokens_spent() >= plan.budget_tokens_total:
            outcome.reason = (
                f"budget total epuise ({plan.tokens_spent()}/{plan.budget_tokens_total} tokens)"
            )
            outcome.stopped_early = True
            break

        task = plan.next_runnable(retry_blocked=retry_blocked, skip=deja_tentees)
        if task is None:
            restantes = plan.counts()["pending"]
            outcome.reason = (
                "toutes les taches sont traitees"
                if not restantes
                else f"{restantes} tache(s) pending bloquees par des dependances non satisfaites"
            )
            break

        deja_tentees.add(task.id)
        on_event("task_start", {"task": task})
        started = utcnow()
        t0 = time.monotonic()
        pack = build_toolpack(plan.toolpack, cfg, task.scope)
        result = run_task(plan, task, pack, provider, cfg, store)
        result = apply_gate(result, task, pack, binding=cfg.checks.binding, on_event=on_event)
        duration = time.monotonic() - t0

        # une seule ecriture atomique : finding + etat + run_log
        store.commit(
            result,
            store.run_entry(task.id, result.status, started, duration, result.tokens),
        )
        outcome.results.append(result)
        outcome.tasks_run += 1
        outcome.tokens += result.tokens
        on_event("task_end", {"task": task, "result": result, "duration": duration})

        # --- circuit breaker
        consecutive_blocked = consecutive_blocked + 1 if result.status == "blocked" else 0
        if consecutive_blocked >= cfg.loop.max_consecutive_blocked:
            outcome.reason = f"{consecutive_blocked} taches bloquees consecutives"
            outcome.stopped_early = True
            break

        # --- critere d'arret (TROU 2)
        if cfg.stop.strategy != "none" or not stop_none_logged:
            entry, tokens = evaluate_stop(store.load(), cfg, provider, task.id)
            stop_none_logged = stop_none_logged or entry.strategy == "none"
            store.mutate(lambda p, e=entry: p.logbook.stop_log.append(e))
            if tokens:
                store.backfill_run_log("stop_check", "done", started, 0.0, tokens)
                outcome.tokens += tokens
            on_event("stop_eval", {"entry": entry})
            if entry.fired:
                outcome.reason = f"critere d'arret declenche: {entry.reason}"
                outcome.stopped_early = True
                break

        if single_step:
            # `inv step` est un outil de debug : il ne cloture pas l'enquete.
            outcome.reason = "arret demande apres une seule tache (inv step)"
            on_event("loop_end", {"outcome": outcome})
            return outcome

    store.set_status("awaiting_review")
    on_event("loop_end", {"outcome": outcome})
    return outcome
