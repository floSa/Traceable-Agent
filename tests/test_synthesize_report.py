"""Etape 6 — synthese (gate humaine, snapshots immuables) et rapport (sans LLM)."""

from __future__ import annotations

import ast
import inspect
import json
import time
from pathlib import Path

import pytest
from conftest import make_plan

from engine import reporter as reporter_module
from engine.brief import bounded_draft_schema, build_brief, slugify, to_plan
from engine.config import Config
from engine.plan import PlanStore
from engine.providers import FakeProvider
from engine.reporter import build_report
from engine.schemas import BriefDraft, Finding, TaskDraft
from engine.synthesize import APPROVAL_STEP, is_approved, render_logbook, synthesize

SYNTH = {
    "headline": "Le churn du T3 vient de la latence API.",
    "answer": "Deux notes concordent.",
    "evidence": ["t1 : churn +12% au T3", "t2 : latence doublee"],
    "caveats": ["t3 bloquee, segment PME non couvert"],
    "recommendation": "Instrumenter la latence avant d'agir.",
}


@pytest.fixture
def enquete(tmp_path: Path) -> PlanStore:
    plan = make_plan(n_tasks=2)
    plan.status = "awaiting_review"
    for t in plan.brief.tasks:
        t.state = "done"
    plan.logbook.findings = [
        Finding(task_id="t1", summary="churn +12%", insight="Le churn monte au T3.", influence="fort"),
        Finding(task_id="t2", summary="latence x2", insight="La latence a double.", influence="fort"),
    ]
    return PlanStore.create(tmp_path / "inv", plan)


# ---- gate humaine --------------------------------------------------------


def test_synthese_refusee_sans_approbation(enquete: PlanStore) -> None:
    with pytest.raises(PermissionError, match="inv approve"):
        synthesize(enquete, Config(), FakeProvider(script=[SYNTH]))


def test_synthese_apres_approbation(enquete: PlanStore) -> None:
    enquete.add_adjustment(APPROVAL_STEP, "ok", "revue manuelle")
    assert is_approved(enquete.load())

    result, path, usage = synthesize(enquete, Config(), FakeProvider(script=[SYNTH]))
    assert result.headline.startswith("Le churn")
    assert path.exists() and path.parent.name == "results"
    assert usage.total > 0
    assert any(e.task_id == "synthesize" for e in enquete.load().logbook.run_log)


def test_les_snapshots_ne_s_ecrasent_jamais(enquete: PlanStore) -> None:
    enquete.add_adjustment(APPROVAL_STEP, "ok", "")
    _, p1, _ = synthesize(enquete, Config(), FakeProvider(script=[SYNTH]))
    _, p2, _ = synthesize(enquete, Config(), FakeProvider(script=[{**SYNTH, "headline": "autre"}]))

    assert p1 != p2 and p1.exists() and p2.exists()
    assert json.loads(p1.read_text())["synthesis"]["headline"].startswith("Le churn")
    assert json.loads(p2.read_text())["synthesis"]["headline"] == "autre"


def test_le_snapshot_est_date_et_trace_le_modele(enquete: PlanStore) -> None:
    enquete.add_adjustment(APPROVAL_STEP, "ok", "")
    _, path, _ = synthesize(enquete, Config(), FakeProvider(script=[SYNTH]))
    payload = json.loads(path.read_text())
    assert payload["slug"] == "demo" and payload["model"] and payload["generated_utc"]
    assert payload["tasks"] == {"t1": "done", "t2": "done"}


def test_la_synthese_voit_le_logbook_mais_pas_les_artefacts(enquete: PlanStore) -> None:
    plan = enquete.load()
    plan.logbook.findings[0].artifacts = ["artifacts/t1_01_grep.txt"]
    enquete.save(plan)
    rendu = render_logbook(enquete.load())
    assert "churn +12%" in rendu and "La latence a double." in rendu
    assert "artifacts/" not in rendu


# ---- rapport -------------------------------------------------------------


def test_le_reporter_n_appelle_aucun_llm() -> None:
    tree = ast.parse(inspect.getsource(reporter_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "complete"


def test_rapport_html_sans_synthese(enquete: PlanStore) -> None:
    html = build_report(enquete).read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "Investigation de demonstration" in html
    assert "inv synthesize" in html  # invite a passer les gates


def test_rapport_html_avec_synthese(enquete: PlanStore) -> None:
    enquete.add_adjustment(APPROVAL_STEP, "ok", "")
    synthesize(enquete, Config(), FakeProvider(script=[SYNTH]))
    time.sleep(1.05)  # horodatage a la seconde : garantit deux noms distincts
    html = build_report(enquete).read_text(encoding="utf-8")

    assert "Le churn du T3 vient de la latence API." in html
    assert "t3 bloquee" in html
    assert "Le churn monte au T3." in html  # les findings brutes restent visibles


def test_le_rapport_est_aussi_un_snapshot(enquete: PlanStore) -> None:
    p1 = build_report(enquete)
    time.sleep(1.05)
    p2 = build_report(enquete)
    assert p1 != p2 and p1.exists() and p2.exists()


# ---- role 1 : brief_builder ---------------------------------------------


DRAFT = {
    "title": "Origine du churn T3",
    "question": "Pourquoi le churn a-t-il augmente au T3 ?",
    "decision": "faut-il investir sur la latence",
    "action_threshold": "2 signaux concordants",
    "kill_criterion": "aucun signal dans les 3 premieres taches",
    "watch_outs": ["ne pas extrapoler du segment PME"],
    "deliverable": "note d'une page",
    "tasks": [
        {"id": "t1", "title": "Reperer les mentions", "question": "Ou parle-t-on de churn ?",
         "scope": {"globs": ["notes/**/*.md"]}, "expected_shape": "liste", "sanity_check": "non vide",
         "depends_on": []},
        {"id": "t2", "title": "Chercher les causes", "question": "Quelles causes citees ?",
         "scope": {"globs": ["notes/**/*.md"]}, "expected_shape": "liste", "sanity_check": "non vide",
         "depends_on": ["t1"]},
        {"id": "t3", "title": "Chercher les contre-exemples", "question": "Qui ne churne pas ?",
         "scope": {"globs": ["notes/**/*.md"]}, "expected_shape": "liste", "sanity_check": "non vide",
         "depends_on": []},
    ],
}


def test_le_brief_produit_un_plan_ready() -> None:
    provider = FakeProvider(script=[DRAFT])
    plan, usage = build_brief("pourquoi le churn ?", "outils fichiers", provider, Config())

    assert plan.status == "ready"  # jamais in_progress : GATE 1 d'abord
    assert plan.mode == "decision"
    assert [t.id for t in plan.brief.tasks] == ["t1", "t2", "t3"]
    assert plan.brief.tasks[1].depends_on == ["t1"]
    # la question tapee par l'humain fait autorite, pas la reformulation du modele
    assert plan.brief.question == "pourquoi le churn ?"
    assert plan.slug == "pourquoi-le-churn"
    assert plan.title == "Origine du churn T3"
    assert usage.total > 0


def test_le_brief_pose_une_question_a_la_fois() -> None:
    provider = FakeProvider(
        script=[
            {"need_more": True, "question": "Sur quelle periode ?"},
            {"need_more": False, "question": ""},
            DRAFT,
        ]
    )
    posees: list[str] = []

    def repondre(q: str) -> str:
        posees.append(q)
        return "le T3 2026"

    build_brief("pourquoi le churn ?", "outils", provider, Config(), ask_human=repondre)
    assert posees == ["Sur quelle periode ?"]
    schemas = [s for s, _, _ in provider.prompts]
    assert schemas == ["ClarifyTurn", "ClarifyTurn", "BoundedBriefDraft"]


def test_les_rounds_de_clarification_sont_bornes() -> None:
    provider = FakeProvider(
        handler=lambda s, u, schema, n: (
            DRAFT if issubclass(schema, BriefDraft) else {"need_more": True, "question": "encore ?"}
        )
    )
    cfg = Config()
    cfg.brief.max_rounds = 2
    posees: list[str] = []
    build_brief("q", "outils", provider, cfg, ask_human=lambda q: posees.append(q) or "oui")
    assert len(posees) == 2


def test_un_brief_trop_maigre_est_renvoye_au_modele() -> None:
    """Le plancher de taches vit dans le SCHEMA : un brief a une tache est une
    erreur de validation, donc il repart au modele avec le motif du rejet."""
    maigre = {**DRAFT, "tasks": DRAFT["tasks"][:1]}
    provider = FakeProvider(script=[maigre, DRAFT])
    plan, _ = build_brief("pourquoi le churn ?", "outils", provider, Config())

    assert len(plan.brief.tasks) == 3
    _, _, second = provider.prompts[1]
    assert "REJETEE" in second and "at least 3 items" in second


def test_les_dependances_hallucinees_sont_elaguees() -> None:
    draft = BriefDraft(
        title="X",
        question="Q",
        tasks=[
            TaskDraft(id="t1", title="A", question="?", depends_on=["t9"]),
            TaskDraft(id="t2", title="B", question="?", depends_on=["t1", "t3"]),
        ],
    )
    plan = to_plan(draft, "files", "fake", question="Q ?")
    assert plan.brief.tasks[0].depends_on == []
    assert plan.brief.tasks[1].depends_on == ["t1"]  # t3 n'existe pas encore


def test_slugify() -> None:
    assert slugify("Pourquoi le churn a-t-il augmenté au T3 ?") == "pourquoi-le-churn-a-t-il-augmente-au-t3"
    assert slugify("!!!") == "investigation"
    assert len(slugify("mot " * 50)) <= 40 and not slugify("mot " * 50).endswith("-")


# ---- garde-fous de schema constates en run reel --------------------------


def test_le_scope_est_type_par_le_pack_et_non_libre() -> None:
    """Constate avec gemma4:e4b : face a un `dict` libre il rend `{}` sur 9
    taches sur 9, meme apres trois retries. Type par le pack, le champ porte
    lui-meme son format — et un scope vide devient une erreur de validation."""
    from engine.toolpacks.files import FilesScope

    schema = bounded_draft_schema(Config(), FilesScope).model_json_schema()
    scope = schema["$defs"]["BoundedTaskDraft"]["properties"]["scope"]
    assert scope == {"$ref": "#/$defs/FilesScope"}  # plus un objet libre
    assert "scope" in schema["$defs"]["BoundedTaskDraft"]["required"]

    sans_scope = {**DRAFT, "tasks": [{**t, "scope": {}} for t in DRAFT["tasks"]]}
    provider = FakeProvider(script=[sans_scope, DRAFT])
    plan, _ = build_brief(
        "pourquoi le churn ?", "outils", provider, Config(), scope_model=FilesScope
    )

    assert all(t.scope.get("globs") for t in plan.brief.tasks)
    assert all(isinstance(t.scope, dict) for t in plan.brief.tasks)  # dict libre dans le plan
    _, _, second = provider.prompts[1]
    assert "globs" in second and "Field required" in second


def test_un_champ_du_brief_promu_en_tache_est_refuse() -> None:
    """Constate aussi en reel : `kill_criterion` remonte comme une tache."""
    pollue = {
        **DRAFT,
        "tasks": DRAFT["tasks"] + [
            {"id": "kill_criterion", "title": "L'enquete peut etre arretee si...",
             "question": "?", "scope": {"globs": ["notes/**/*.md"]},
             "expected_shape": "", "sanity_check": "", "depends_on": []}
        ],
    }
    provider = FakeProvider(script=[pollue, DRAFT])
    plan, _ = build_brief("pourquoi le churn ?", "outils", provider, Config())

    assert [t.id for t in plan.brief.tasks] == ["t1", "t2", "t3"]
    _, _, second = provider.prompts[1]
    assert "est un champ du brief, pas une tache" in second


def test_chaque_pack_declare_son_propre_schema_de_scope(corpus: Path) -> None:
    """Le moteur ne sait pas ce qu'est un perimetre : le pack le declare."""
    from engine.config import FilesPackConfig, ToolpackConfig
    from engine.toolpacks.base import build_toolpack

    cfg = Config(toolpack=ToolpackConfig(files=FilesPackConfig(root=str(corpus))))
    cfg.root = corpus.parent
    champs = build_toolpack("files", cfg, {}).scope_model().model_fields
    assert list(champs) == ["globs"]
