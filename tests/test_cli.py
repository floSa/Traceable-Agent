"""Etape 6b — la CLI, et surtout les deux points de controle humain."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_plan
from typer.testing import CliRunner

from engine import cli
from engine.plan import PlanStore
from engine.providers import FakeProvider
from engine.schemas import Finding

runner = CliRunner()

FINAL = {
    "action": "final_answer",
    "summary": "notes/a.md:2 mentionne un churn de 12%",
    "insight": "Le churn augmente au T3.",
    "influence": "confirme",
}
SYNTH = {
    "headline": "Le churn vient de la latence.",
    "answer": "Deux notes concordent.",
    "evidence": ["t1"],
    "caveats": [],
    "recommendation": "instrumenter",
}
DRAFT = {
    "title": "Origine du churn",
    "question": "Pourquoi le churn augmente ?",
    "decision": "",
    "action_threshold": "",
    "kill_criterion": "",
    "watch_outs": [],
    "deliverable": "note",
    "tasks": [
        {"id": "t1", "title": "Reperer", "question": "Ou parle-t-on de churn ?",
         "scope": {"globs": ["notes/**/*.md"]}, "expected_shape": "liste",
         "sanity_check": "non vide", "depends_on": []},
        {"id": "t2", "title": "Causes", "question": "Quelles causes citees ?",
         "scope": {"globs": ["notes/**/*.md"]}, "expected_shape": "liste",
         "sanity_check": "non vide", "depends_on": ["t1"]},
        {"id": "t3", "title": "Contre-exemples", "question": "Qui ne churne pas ?",
         "scope": {"globs": ["notes/**/*.md"]}, "expected_shape": "liste",
         "sanity_check": "non vide", "depends_on": []},
    ],
}


@pytest.fixture
def projet(tmp_path: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Un projet complet : config.yml pointant sur le corpus de test."""
    (tmp_path / "config.yml").write_text(
        "paths:\n  investigations: ./investigations\n"
        "toolpack:\n  default: files\n"
        f"  files:\n    root: {corpus}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def fake(script) -> None:
    """Injecte un provider scripte a la place du service LLM."""
    provider = FakeProvider(script=script)
    cli._provider = lambda cfg: provider  # type: ignore[assignment]


def invoke(*args: str):
    result = runner.invoke(cli.app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def enquete_prete(projet: Path, corpus: Path) -> PlanStore:
    plan = make_plan(n_tasks=2)
    plan.status = "ready"
    for t in plan.brief.tasks:
        t.scope = {"globs": ["notes/**/*.md"]}
    return PlanStore.create(projet / "investigations", plan)


# ---- parcours complet ----------------------------------------------------


def test_parcours_complet(projet: Path, corpus: Path) -> None:
    fake([DRAFT])
    r = invoke("new", "Pourquoi le churn augmente ?", "--yes")
    assert r.exit_code == 0 and "GATE 1" in r.stdout
    slug = "pourquoi-le-churn-augmente"  # issu de la question tapee, pas du titre LLM

    assert (projet / "investigations" / "README.md").exists()  # registre tenu a jour

    r = invoke("tasks", slug)
    assert r.exit_code == 0 and "Reperer" in r.stdout

    fake([FINAL] * 3)
    r = invoke("run", slug)
    assert r.exit_code == 0 and "GATE 2" in r.stdout

    r = invoke("status", slug)
    assert "awaiting_review" in r.stdout and "3 done" in r.stdout

    r = invoke("review", slug)
    assert "Le churn augmente au T3." in r.stdout

    r = invoke("approve", slug)
    assert r.exit_code == 0

    fake([SYNTH])
    r = invoke("synthesize", slug)
    assert r.exit_code == 0 and "Le churn vient de la latence." in r.stdout

    r = invoke("report", slug)
    assert r.exit_code == 0 and "rapport ecrit" in r.stdout

    r = invoke("done", slug)
    assert r.exit_code == 0
    store = PlanStore(next((projet / "investigations").glob(f"*_{slug}")))
    assert store.load().status == "done"
    assert list(store.results_dir.glob("*_synthesis.json"))
    assert list(store.results_dir.glob("*_report.html"))


# ---- les gates ne sont pas contournables --------------------------------


def test_run_refuse_un_plan_en_draft(projet: Path, corpus: Path) -> None:
    plan = make_plan(n_tasks=1)
    plan.status = "draft"
    PlanStore.create(projet / "investigations", plan)
    r = invoke("run", "demo")
    assert r.exit_code == 1 and "draft" in r.stdout


def test_synthesize_refuse_sans_approve(projet: Path, corpus: Path) -> None:
    store = enquete_prete(projet, corpus)
    fake([FINAL, FINAL])
    invoke("run", "demo")
    fake([SYNTH])
    r = invoke("synthesize", "demo")
    assert r.exit_code == 1 and "inv approve" in r.stdout
    assert not list(store.results_dir.glob("*_synthesis.json"))


def test_done_refuse_sans_approve(projet: Path, corpus: Path) -> None:
    store = enquete_prete(projet, corpus)
    r = invoke("done", "demo")
    assert r.exit_code == 1
    assert store.load().status != "done"


def test_aucune_commande_ne_passe_a_done_toute_seule(projet: Path, corpus: Path) -> None:
    store = enquete_prete(projet, corpus)
    fake([FINAL, FINAL])
    invoke("run", "demo")
    invoke("approve", "demo")
    fake([SYNTH])
    invoke("synthesize", "demo")
    invoke("report", "demo")
    assert store.load().status == "awaiting_review"  # toujours pas `done`


def test_review_avant_run_ne_montre_rien(projet: Path, corpus: Path) -> None:
    enquete_prete(projet, corpus)
    r = invoke("review", "demo")
    assert r.exit_code == 1 and "aucune finding" in r.stdout


# ---- divers --------------------------------------------------------------


def test_step_execute_une_seule_tache(projet: Path, corpus: Path) -> None:
    store = enquete_prete(projet, corpus)
    fake([FINAL])
    r = invoke("step", "demo")
    assert r.exit_code == 0
    assert store.load().counts() == {"pending": 1, "done": 1, "skipped": 0, "blocked": 0}
    assert "GATE 2" not in r.stdout


def test_slug_inconnu(projet: Path) -> None:
    assert invoke("status", "fantome").exit_code == 1


def test_toolpack_inconnu(projet: Path) -> None:
    r = invoke("new", "q", "--toolpack", "mongo", "--yes")
    assert r.exit_code == 1 and "toolpack inconnu" in r.stdout


def test_kill_trace_la_raison(projet: Path, corpus: Path) -> None:
    store = enquete_prete(projet, corpus)
    r = invoke("kill", "demo", "--reason", "corpus indisponible")
    assert r.exit_code == 0
    plan = store.load()
    assert plan.status == "killed"
    assert plan.logbook.adjustments[-1].reason == "corpus indisponible"


def test_list_affiche_les_enquetes(projet: Path, corpus: Path) -> None:
    enquete_prete(projet, corpus)
    r = invoke("list")
    assert "demo" in r.stdout and "ready" in r.stdout


def test_status_affiche_les_evaluations_d_arret(projet: Path, corpus: Path) -> None:
    store = enquete_prete(projet, corpus)
    store.mutate(
        lambda p: p.logbook.findings.append(Finding(task_id="t1", insight="x"))
    )
    fake([FINAL, FINAL])
    invoke("run", "demo")
    r = invoke("status", "demo")
    sortie = " ".join(r.stdout.split())  # rich retaille les lignes
    assert "arret apres t1 · none · fired=False" in sortie
    assert "jamais evalue" in sortie
