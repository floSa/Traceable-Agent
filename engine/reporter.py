"""Role 5 — `reporter` : AUCUN LLM, juste Jinja2.

Prend le plan et la derniere synthese sur disque, rend un HTML autonome.
Le rapport est lui aussi un snapshot date : on n'ecrase jamais le precedent.
"""

from __future__ import annotations

from pathlib import Path

from engine.plan import PlanStore, jinja_env
from engine.synthesize import latest_synthesis, snapshot_path


def build_report(store: PlanStore) -> Path:
    plan = store.load()
    synthese = latest_synthesis(store)

    html = jinja_env().get_template("report.html.j2").render(
        plan=plan,
        synthese=(synthese or {}).get("synthesis"),
        meta=synthese or {},
        findings={f.task_id: f for f in plan.logbook.findings},
    )
    path = snapshot_path(store, "report.html")
    path.write_text(html, encoding="utf-8")
    return path
