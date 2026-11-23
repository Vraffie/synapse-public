"""Render the desktop Control Center offscreen with representative FIXTURE data and save PNG screenshots.

Usage: QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/render_control_center.py [out_dir]

The fixture exists only here (and in tests); the real view is fed exclusively by daemon data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SYNAPSE_REDUCED_MOTION", "1")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

NODES = [("plan-scope", "completed", []), ("edit-config", "completed", ["plan-scope"]), ("update-docs", "running", ["plan-scope"]),
         ("run-lens", "failed", ["plan-scope"]), ("collect-evidence", "running", ["plan-scope"]),
         ("review-diff", "changes_required", ["edit-config", "update-docs"]),
         ("qa-verify", "waiting_dependencies", ["run-lens", "review-diff"]), ("deploy-verify", "awaiting_approval", ["qa-verify", "collect-evidence"])]


def fixture():
    tasks = [{"id": f"t-{n}", "node_id": n, "state": s, "state_version": 1, "attempt_count": 2 if s == "failed" else 1} for n, s, _ in NODES]
    deps = [{"task_id": f"t-{n}", "depends_on_id": f"t-{d}"} for n, _s, ds in NODES for d in ds]
    run = {"id": "run-1", "workflow_id": "catalog-change", "state": "awaiting_approval", "created_at": "2026-11-20T09:14:00Z"}
    approvals = [{"id": "ap-1", "run_id": "run-1", "operation": "deploy to staging", "target": "catalog-config:1f2e3d4c", "state": "requested"}]
    messages = [{"id": "m1", "type": "task.request", "created_at": "2026-11-20T09:14:05Z", "payload": {"task_id": "t-edit-config"}},
                {"id": "m2", "type": "change.completed", "created_at": "2026-11-20T09:20:10Z", "payload": {"task_id": "t-edit-config"}},
                {"id": "m3", "type": "review.request", "created_at": "2026-11-20T09:21:00Z", "payload": {"task_id": "t-review-diff"}},
                {"id": "m4", "type": "review.finding", "created_at": "2026-11-20T09:22:30Z", "payload": {"task_id": "t-review-diff", "approved": False}}]
    events = [{"sequence": 1, "type": "task.state_changed", "run_id": "run-1", "aggregate_id": "t-run-lens", "occurred_at": "2026-11-20T09:23:00Z",
               "payload": {"before": "running", "after": "failed"}},
              {"sequence": 2, "type": "approval.requested", "run_id": "run-1", "aggregate_id": "ap-1", "occurred_at": "2026-11-20T09:24:00Z",
               "payload": {"operation": "deploy to staging"}}]
    detail = {"outcome": None, "summary_text": "Updated the notification contact mapping in two catalog files.", "tool_names": ["Read", "Edit"], "tools_total": 2,
              "worktree": {"status": "dirty", "head": "abc1234", "diff_stat": "2 files changed, 14 insertions(+), 3 deletions(-)", "diff": "", "truncated": False}}
    return run, tasks, deps, approvals, messages, events, detail


def build(settings=None):
    from synapse.desktop.control_center import ControlCenter
    center = ControlCenter(settings=settings)
    run, tasks, deps, approvals, messages, events, detail = fixture()
    center.set_run_id("run-1")
    center.set_runs([{**run, "task_counts": {"completed": 2}, "pending_approvals": 1}])
    center.set_snapshot("run-1", {"run": run, "tasks": tasks, "dependencies": deps})
    center.set_approvals(approvals)
    center.set_agent_detail(detail)
    center.set_messages("run-1", messages)
    for event in events:
        center.add_event(event)
    center.set_connection("Connected")
    center.select("t-run-lens")
    return center


def main(out: Path) -> list[Path]:
    from synapse.desktop import theme
    from synapse.desktop.control_center import ControlCenter
    app = QApplication.instance() or QApplication([])
    out.mkdir(parents=True, exist_ok=True)
    ini = out / ".render.ini"
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    written = []
    previous_theme = os.environ.get("SYNAPSE_THEME")
    for name, dark, populated in (("overview", True, True), ("empty", True, False), ("light", False, True)):
        os.environ["SYNAPSE_THEME"] = "dark" if dark else "light"
        theme.apply_theme(app)
        center = build(settings) if populated else ControlCenter(settings=settings)
        center.resize(1280, 940)
        center.show()
        app.processEvents()
        path = out / f"control-center-{name}.png"
        center.grab().scaledToWidth(1100).save(str(path), "PNG")
        written.append(path)
        center.close()
    if previous_theme is None:
        os.environ.pop("SYNAPSE_THEME", None)
    else:
        os.environ["SYNAPSE_THEME"] = previous_theme
    ini.unlink(missing_ok=True)
    return written


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent.parent / "docs" / "operations" / "img"
    for item in main(Path(sys.argv[1]) if len(sys.argv) > 1 else default_out):
        print(item)
