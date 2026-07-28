"""Etape 5 — l'orchestrateur : arrets, circuit breaker, reprise."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from conftest import make_plan

from engine import loop as loop_module
from engine.config import AgentConfig, Config, FilesPackConfig, LoopConfig, StopConfig, ToolpackConfig
from engine.loop import run
from engine.plan import PlanStore
from engine.providers import FakeProvider
from engine.schemas import Plan, StopEvaluation

FINAL = {
    "action": "final_answer",
    "summary": "trouve dans notes/a.md",
    "insight": "Le churn augmente au T3.",
    "influence": "confirme",
}
TOOL = {"action": "call_tool", "tool": "grep", "args": {"pattern": "churn", "glob": "notes/**/*.md"}}


def cfg_for(corpus: Path, **kw) -> Config:
    c = Config(toolpack=ToolpackConfig(files=FilesPackConfig(root=str(corpus))), **kw)
    c.root = corpus.parent
    return c


def store_for(tmp_path: Path, n: int = 3, **plan_kw) -> PlanStore:
    return PlanStore.create(tmp_path / "inv", make_plan(n_tasks=n, **plan_kw))


# ---- invariant structurel -----------------------------------------------


def test_la_boucle_ne_contient_aucun_appel_llm() -> None:
    """Verifie l'AST, pas le texte : aucun appel `.complete(...)`, aucun client
    HTTP, aucune constante de prompt dans loop.py."""
    tree = ast.parse(inspect.getsource(loop_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "complete", "loop.py appelle un modele"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            noms = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any("httpx" in n for n in noms), "loop.py importe un client HTTP"
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            valeur = node.value.value
            if isinstance(valeur, str):
                assert len(valeur) < 200, "loop.py contient une constante de prompt"


def test_la_boucle_tient_en_moins_de_200_lignes() -> None:
    code = [
        l
        for l in inspect.getsource(loop_module).splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    assert len(code) < 200, f"{len(code)} lignes de code dans loop.py"


# ---- nominal -------------------------------------------------------------


def test_la_boucle_execute_toutes_les_taches(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 3)
    provider = FakeProvider(script=[FINAL] * 3)
    outcome = run(store, cfg_for(corpus), provider)

    plan = store.load()
    assert outcome.tasks_run == 3
    assert plan.counts()["done"] == 3
    assert plan.status == "awaiting_review"
    assert len(plan.logbook.run_log) == 3
    assert [e.task_id for e in plan.logbook.run_log] == ["t1", "t2", "t3"]
    assert plan.tokens_spent() > 0


def test_l_ordre_suit_les_dependances(corpus: Path, tmp_path: Path) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=3, chain=True))
    run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 3))
    assert [e.task_id for e in store.load().logbook.run_log] == ["t1", "t2", "t3"]


def test_step_n_execute_qu_une_tache_et_ne_cloture_pas(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 3)
    outcome = run(store, cfg_for(corpus), FakeProvider(script=[FINAL]), single_step=True)
    plan = store.load()
    assert outcome.tasks_run == 1
    assert plan.counts() == {"pending": 2, "done": 1, "skipped": 0, "blocked": 0}
    assert plan.status == "in_progress"


# ---- arrets --------------------------------------------------------------


def test_deux_blocked_consecutifs_ouvrent_le_circuit(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 4)
    cfg = cfg_for(corpus, agent=AgentConfig(max_tool_calls=1), loop=LoopConfig(max_consecutive_blocked=2))
    # chaque tache consomme son unique tour en appelant un outil -> blocked
    outcome = run(store, cfg, FakeProvider(script=[TOOL] * 10))

    assert outcome.stopped_early
    assert "2 taches bloquees consecutives" in outcome.reason
    plan = store.load()
    assert plan.counts()["blocked"] == 2
    assert plan.counts()["pending"] == 2  # la boucle s'est arretee net


def test_un_succes_remet_le_compteur_de_blocked_a_zero(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 4)
    cfg = cfg_for(corpus, agent=AgentConfig(max_tool_calls=1), loop=LoopConfig(max_consecutive_blocked=2))
    outcome = run(store, cfg, FakeProvider(script=[TOOL, FINAL, TOOL, TOOL]))

    assert outcome.tasks_run == 4
    assert store.load().counts() == {"pending": 0, "done": 1, "skipped": 0, "blocked": 3}
    assert "2 taches bloquees consecutives" in outcome.reason


def test_budget_total_epuise_arrete_la_boucle(corpus: Path, tmp_path: Path) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=5, budget_tokens_total=250))
    outcome = run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 5, tokens_per_call=100))

    assert outcome.stopped_early and "budget total epuise" in outcome.reason
    assert store.load().counts()["pending"] > 0


def test_taches_bloquees_par_leurs_dependances(corpus: Path, tmp_path: Path) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=3, chain=True))
    cfg = cfg_for(corpus, agent=AgentConfig(max_tool_calls=1))
    run(store, cfg, FakeProvider(script=[TOOL] * 4))
    plan = store.load()
    assert plan.task("t1").state == "blocked"
    assert plan.counts()["pending"] == 2  # t2 et t3 ne seront jamais lancees


# ---- TROU 2 : critere d'arret -------------------------------------------


def test_strategie_none_est_journalisee_une_fois(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 3)
    run(store, cfg_for(corpus, stop=StopConfig(strategy="none")), FakeProvider(script=[FINAL] * 3))
    log = store.load().logbook.stop_log
    assert len(log) == 1
    assert log[0].strategy == "none" and log[0].fired is False
    assert "jamais evalue" in log[0].reason


def test_strategie_callable_declenche_et_est_journalisee(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 4)
    cfg = cfg_for(corpus, stop=StopConfig(strategy="callable", callable=f"{__name__}:stop_apres_deux"))
    outcome = run(store, cfg, FakeProvider(script=[FINAL] * 4))

    assert outcome.stopped_early and "critere d'arret declenche" in outcome.reason
    log = store.load().logbook.stop_log
    assert [e.fired for e in log] == [False, True]
    assert all(e.strategy == "callable" for e in log)


def stop_apres_deux(plan: Plan) -> StopEvaluation:
    n = plan.counts()["done"]
    return StopEvaluation(fired=n >= 2, reason=f"{n} taches faites")


def test_un_critere_qui_plante_n_arrete_pas_l_enquete(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 2)
    cfg = cfg_for(corpus, stop=StopConfig(strategy="callable", callable=f"{__name__}:stop_casse"))
    outcome = run(store, cfg, FakeProvider(script=[FINAL] * 2))

    assert outcome.tasks_run == 2 and not outcome.stopped_early
    assert "critere en erreur" in store.load().logbook.stop_log[0].reason


def stop_casse(plan: Plan) -> bool:
    raise RuntimeError("boom")


def test_strategie_llm_consomme_du_budget_et_journalise(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 2)
    cfg = cfg_for(corpus, stop=StopConfig(strategy="llm"))
    provider = FakeProvider(
        script=[FINAL, {"fired": False, "reason": "trop tot"}, FINAL, {"fired": True, "reason": "assez"}]
    )
    outcome = run(store, cfg, provider)

    plan = store.load()
    assert [e.fired for e in plan.logbook.stop_log] == [False, True]
    assert "assez" in outcome.reason
    assert any(e.task_id == "stop_check" for e in plan.logbook.run_log)


# ---- TROU 6 : reprise ----------------------------------------------------


def test_relance_reprend_a_la_premiere_pending(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 4)
    run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 2), single_step=True)
    run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 3))

    plan = store.load()
    assert plan.counts()["done"] == 4
    assert [e.task_id for e in plan.logbook.run_log] == ["t1", "t2", "t3", "t4"]
    assert len(plan.logbook.findings) == 4  # aucune tache rejouee


def test_retry_blocked_rejoue_les_taches_bloquees(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 2)
    cfg_bloquant = cfg_for(corpus, agent=AgentConfig(max_tool_calls=1))
    run(store, cfg_bloquant, FakeProvider(script=[TOOL] * 4))
    assert store.load().counts()["blocked"] == 2

    run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 2), retry_blocked=True)
    plan = store.load()
    assert plan.counts() == {"pending": 0, "done": 2, "skipped": 0, "blocked": 0}
    assert len(plan.logbook.findings) == 2  # les findings sont remplacees, pas dupliquees


def test_sans_retry_blocked_les_taches_bloquees_sont_ignorees(corpus: Path, tmp_path: Path) -> None:
    store = store_for(tmp_path, 2)
    bloquant = cfg_for(corpus, agent=AgentConfig(max_tool_calls=1))
    run(store, bloquant, FakeProvider(script=[TOOL]), single_step=True)  # t1 -> blocked
    run(store, cfg_for(corpus), FakeProvider(script=[FINAL]))  # reprend a t2

    plan = store.load()
    assert plan.task("t1").state == "blocked" and plan.task("t2").state == "done"


def test_une_tache_n_est_tentee_qu_une_fois_par_run(corpus: Path, tmp_path: Path) -> None:
    """Constate en run reel : sous --retry-blocked, une tache qui rebloque
    restait eligible et se faisait rejouer en boucle."""
    store = store_for(tmp_path, 3)
    bloquant = cfg_for(corpus, agent=AgentConfig(max_tool_calls=1))
    run(store, bloquant, FakeProvider(script=[TOOL] * 2))  # t1 et t2 -> blocked

    outcome = run(store, bloquant, FakeProvider(script=[TOOL] * 10), retry_blocked=True)
    tentees = [r.task_id for r in outcome.results]
    assert tentees == ["t1", "t2"]  # chacune une seule fois, pas de boucle
    assert len(tentees) == len(set(tentees))


def test_le_run_suivant_repart_des_blocked(corpus: Path, tmp_path: Path) -> None:
    """Le skip est propre au run : relancer redonne leur chance aux blocked."""
    store = store_for(tmp_path, 2)
    run(store, cfg_for(corpus, agent=AgentConfig(max_tool_calls=1)), FakeProvider(script=[TOOL] * 2))
    assert store.load().counts()["blocked"] == 2

    run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 2), retry_blocked=True)
    assert store.load().counts()["done"] == 2
