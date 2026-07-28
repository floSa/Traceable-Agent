"""Etape 1 — robustesse de l'ecriture du plan."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import make_plan
from pydantic import ValidationError

from engine.plan import PlanStore, find_investigation
from engine.schemas import Brief, Plan, Task, TaskResult, utcnow


def test_roundtrip_sans_perte(store: PlanStore) -> None:
    before = store.load()
    store.save(before)
    after = store.load()
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_le_frontmatter_est_la_source_de_verite(store: PlanStore) -> None:
    text = store.plan_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    # le corps markdown est une vue generee, presente mais jamais relue
    assert "## Logbook" in text
    assert "Investigation de demonstration" in text


def test_cent_commits_successifs_gardent_le_plan_valide(tmp_path: Path) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=100))
    for i in range(1, 101):
        store.commit(
            TaskResult(
                task_id=f"t{i}",
                status="done",
                summary=f"resume {i}",
                insight=f"insight {i}",
                influence="faible",
                artifacts=[f"artifacts/t{i}_00_grep.txt"],
                tokens=10 * i,
            )
        )
        store.backfill_run_log(f"t{i}", "done", utcnow(), 0.01 * i, 10 * i)

    plan = store.load()  # relecture + revalidation integrale
    assert len(plan.logbook.findings) == 100
    assert len(plan.logbook.run_log) == 100
    assert plan.counts() == {"pending": 0, "done": 100, "skipped": 0, "blocked": 0}
    assert plan.tokens_spent() == sum(10 * i for i in range(1, 101))
    assert all(f.chart is None for f in plan.logbook.findings)  # TROU 5


def test_commit_est_idempotent_sur_le_finding(store: PlanStore) -> None:
    store.commit(TaskResult(task_id="t1", status="blocked", reason="budget", insight=""))
    store.commit(TaskResult(task_id="t1", status="done", insight="finalement ok"))
    plan = store.load()
    assert len([f for f in plan.logbook.findings if f.task_id == "t1"]) == 1
    assert plan.task("t1").state == "done"
    assert plan.finding("t1").insight == "finalement ok"


def test_commit_sur_tache_inconnue_refuse(store: PlanStore) -> None:
    with pytest.raises(KeyError):
        store.commit(TaskResult(task_id="inexistante", status="done"))


def test_ecriture_atomique_pas_de_tmp_resident(store: PlanStore) -> None:
    store.commit(TaskResult(task_id="t1", status="done", insight="x"))
    restes = [p.name for p in store.dir.iterdir() if p.name.endswith(".tmp")]
    assert restes == []


def test_un_fichier_corrompu_est_refuse_a_la_lecture(store: PlanStore) -> None:
    store.plan_path.write_text("---\nslug: demo\n---\ncorps\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        store.load()


def test_schema_refuse_un_champ_inconnu(store: PlanStore) -> None:
    raw = store.plan_path.read_text(encoding="utf-8")
    store.plan_path.write_text(raw.replace("slug:", "champ_inconnu: 1\nslug:", 1), encoding="utf-8")
    with pytest.raises(ValidationError):
        store.load()


def test_graphe_de_taches_incoherent_refuse() -> None:
    with pytest.raises(ValidationError):
        Brief(tasks=[Task(id="a", title="A", question="?", depends_on=["zzz"])])
    with pytest.raises(ValidationError):
        Brief(tasks=[Task(id="a", title="A", question="?"), Task(id="a", title="A2", question="?")])


def test_next_runnable_respecte_les_dependances(tmp_path: Path) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=3, chain=True))
    plan = store.load()
    assert plan.next_runnable().id == "t1"
    store.commit(TaskResult(task_id="t1", status="done", insight="i1"))
    assert store.load().next_runnable().id == "t2"


def test_next_runnable_saute_les_blocked_sauf_retry(tmp_path: Path) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=2))
    store.commit(TaskResult(task_id="t1", status="blocked", reason="budget"))
    plan = store.load()
    assert plan.next_runnable().id == "t2"
    assert plan.next_runnable(retry_blocked=True).id == "t1"


def test_find_investigation_par_slug(tmp_path: Path) -> None:
    root = tmp_path / "inv"
    PlanStore.create(root, make_plan())
    assert find_investigation(root, "demo").exists()
    with pytest.raises(FileNotFoundError):
        find_investigation(root, "absent")


def test_verrou_exclusif(store: PlanStore) -> None:
    import fcntl

    with store.lock():
        fd = os.open(store.lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_save_revalide_avant_ecriture(store: PlanStore) -> None:
    plan = store.load()
    plan.brief.tasks[0].depends_on = ["fantome"]
    with pytest.raises(ValidationError):
        store.save(plan)
    assert store.load().brief.tasks[0].depends_on == []  # disque intact


def test_artifact_ecrit_hors_du_plan(store: PlanStore) -> None:
    rel = store.write_artifact("t1", 0, "grep", "pattern=churn\n3 hits")
    assert rel == "artifacts/t1_00_grep.txt"
    content = (store.dir / rel).read_text(encoding="utf-8")
    assert "pattern=churn" in content
    assert "task=t1" in content
    # l'artefact n'entre jamais dans le plan
    assert "pattern=churn" not in store.plan_path.read_text(encoding="utf-8")


def test_plan_minimal_valide() -> None:
    p = Plan(slug="x", title="X")
    assert p.status == "draft" and p.toolpack == "files"
