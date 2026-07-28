"""Process-cobaye pour test_crash_recovery.py — lance a part, tue au milieu.

argv : <dossier_investigation> <racine_corpus> <mode>
  mode=loop    : execute la boucle, chaque tache prend ~0.4s
  mode=commits : ecrit des commits en rafale (prouve l'ecriture atomique)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.config import Config, FilesPackConfig, ToolpackConfig  # noqa: E402
from engine.loop import run  # noqa: E402
from engine.plan import PlanStore  # noqa: E402
from engine.providers import FakeProvider  # noqa: E402
from engine.schemas import TaskResult  # noqa: E402

inv_dir, corpus, mode = sys.argv[1], sys.argv[2], sys.argv[3]
store = PlanStore(inv_dir)

if mode == "commits":
    i = 0
    while True:
        i += 1
        tid = f"t{(i % 20) + 1}"
        store.commit(TaskResult(task_id=tid, status="done", insight=f"insight {i}"))
else:
    cfg = Config(toolpack=ToolpackConfig(files=FilesPackConfig(root=corpus)))
    cfg.root = Path(corpus).parent

    def handler(system, user, schema, n):
        time.sleep(0.4)  # laisse au parent le temps de tuer proprement au milieu
        return {
            "action": "final_answer",
            "summary": "trouve dans notes/a.md",
            "insight": "Le churn augmente au T3.",
            "influence": "confirme",
        }

    run(store, cfg, FakeProvider(handler=handler))
