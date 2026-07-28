"""Etape 4a — TROU 1 : ce que le sous-agent a le droit de savoir."""

from __future__ import annotations

from conftest import make_plan

from engine.config import ContextConfig
from engine.context import build_context, first_sentence, render_task
from engine.schemas import Finding


def plan_avec_findings():
    plan = make_plan(n_tasks=3, chain=True)
    plan.logbook.findings = [
        Finding(
            task_id="t1",
            summary="LONG RESUME CONFIDENTIEL de 3 paragraphes qui ne doit pas fuiter",
            insight="Le churn se concentre sur le segment Entreprise. Detail annexe ignore.",
            influence="oriente vers une cause produit",
            artifacts=["artifacts/t1_01_grep.txt"],
        ),
        Finding(
            task_id="t2",
            summary="autre resume qui ne doit pas fuiter",
            insight="La latence API a double au T3.",
            influence="renforce l'hypothese",
        ),
    ]
    plan.logbook.progress = ["t1 -> done", "t2 -> done"]
    plan.logbook.adjustments = []
    return plan


def test_build_context_ne_contient_jamais_le_logbook_complet() -> None:
    plan = plan_avec_findings()
    ctx = build_context(plan, plan.task("t3"))
    assert "CONFIDENTIEL" not in ctx
    assert "resume" not in ctx.lower()
    assert "artifacts/" not in ctx
    assert "run_log" not in ctx
    assert "-> done" not in ctx  # ni la progression
    assert "oriente vers une cause produit" not in ctx  # ni les influences


def test_build_context_contient_question_watchouts_et_insights_des_dependances() -> None:
    plan = plan_avec_findings()
    ctx = build_context(plan, plan.task("t3"))
    assert "Que dit le corpus ?" in ctx
    assert "ne pas extrapoler" in ctx
    assert "La latence API a double au T3." in ctx
    assert "Le churn se concentre" not in ctx  # t3 ne depend que de t2


def test_build_context_ne_remonte_que_les_dependances_declarees() -> None:
    plan = plan_avec_findings()
    plan.task("t3").depends_on = ["t1", "t2"]
    ctx = build_context(plan, plan.task("t3"))
    assert "Le churn se concentre sur le segment Entreprise." in ctx
    assert "Detail annexe ignore" not in ctx  # une seule phrase d'insight


def test_build_context_sans_dependance_est_minimal() -> None:
    plan = plan_avec_findings()
    ctx = build_context(plan, plan.task("t1"))
    assert "Acquis des taches" not in ctx
    assert "Question globale" in ctx


def test_dependance_sans_finding_est_signalee_pas_inventee() -> None:
    plan = make_plan(n_tasks=2, chain=True)
    ctx = build_context(plan, plan.task("t2"))
    assert "aucun insight disponible" in ctx


def test_options_de_config_sont_honorees() -> None:
    plan = plan_avec_findings()
    task = plan.task("t3")
    assert "Acquis" not in build_context(plan, task, ContextConfig(include_dependencies=False))
    assert "vigilance" not in build_context(plan, task, ContextConfig(include_watch_outs=False))
    assert "Question globale" not in build_context(plan, task, ContextConfig(include_question=False))


def test_max_chars_tronque() -> None:
    plan = plan_avec_findings()
    ctx = build_context(plan, plan.task("t3"), ContextConfig(max_chars=80))
    assert len(ctx) <= 80 + len("\n[... contexte tronque]")
    assert "tronque" in ctx


def test_max_dependencies_garde_les_plus_recentes() -> None:
    plan = plan_avec_findings()
    plan.task("t3").depends_on = ["t1", "t2"]
    ctx = build_context(plan, plan.task("t3"), ContextConfig(max_dependencies=1))
    assert "latence API" in ctx and "churn se concentre" not in ctx


def test_build_context_est_pure() -> None:
    """Meme entree -> meme sortie, et l'entree n'est pas modifiee."""
    plan = plan_avec_findings()
    avant = plan.model_dump(mode="json")
    a = build_context(plan, plan.task("t3"))
    b = build_context(plan, plan.task("t3"))
    assert a == b
    assert plan.model_dump(mode="json") == avant


def test_first_sentence() -> None:
    assert first_sentence("Une phrase. Une autre.") == "Une phrase."
    assert first_sentence("  Pas de ponctuation  ") == "Pas de ponctuation"
    assert first_sentence("") == ""


def test_render_task_donne_tout_le_necessaire() -> None:
    plan = plan_avec_findings()
    r = render_task(plan.task("t1"))
    assert "Question 1 ?" in r and "globs" in r and "coherence" in r
    assert "une phrase" in r  # expected_shape
