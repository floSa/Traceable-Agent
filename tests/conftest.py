from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.plan import PlanStore  # noqa: E402
from engine.schemas import Brief, DecisionFrame, Plan, Task  # noqa: E402


def make_plan(n_tasks: int = 3, chain: bool = False, **kw) -> Plan:
    tasks = []
    for i in range(1, n_tasks + 1):
        tasks.append(
            Task(
                id=f"t{i}",
                title=f"Tache {i}",
                question=f"Question {i} ?",
                scope={"globs": ["**/*.md"]},
                expected_shape="une phrase",
                sanity_check="non vide",
                depends_on=[f"t{i - 1}"] if chain and i > 1 else [],
            )
        )
    defaults = dict(
        slug="demo",
        title="Investigation de demonstration",
        toolpack="files",
        model="fake",
        brief=Brief(
            owner="florian",
            question="Que dit le corpus ?",
            decision_frame=DecisionFrame(
                decision="continuer ou non",
                action_threshold="au moins 2 signaux concordants",
                kill_criterion="aucun signal dans les 3 premieres taches",
            ),
            watch_outs=["ne pas extrapoler", "citer les fichiers"],
            deliverable="une note d'une page",
            tasks=tasks,
        ),
    )
    defaults.update(kw)
    return Plan(**defaults)


@pytest.fixture
def store(tmp_path: Path) -> PlanStore:
    return PlanStore.create(tmp_path / "investigations", make_plan())


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Petit corpus de fichiers texte pour le pack `files`."""
    d = tmp_path / "corpus"
    (d / "notes").mkdir(parents=True)
    (d / "notes" / "a.md").write_text(
        "# Note A\nLe churn augmente de 12% au T3.\nCause probable: latence API.\n",
        encoding="utf-8",
    )
    (d / "notes" / "b.md").write_text(
        "# Note B\nAucun churn observe sur le segment PME.\nLatence stable.\n",
        encoding="utf-8",
    )
    (d / "secret.txt").write_text("mot de passe: hunter2\n", encoding="utf-8")
    return d
