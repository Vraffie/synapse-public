"""Smoke test: the demo harness against a REAL daemon subprocess (kill/restart, expiry, backup)."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("demo_mock", ROOT / "scripts" / "demo_mock.py")
demo_mock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo_mock)


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    base = tmp_path_factory.mktemp("demo")
    state = base / "state"
    state.mkdir(mode=0o700)
    demo = demo_mock.Demo(state, quiet=True)
    try:
        report = demo.run_all()
    finally:
        demo.close()
    return demo, report, state


def test_all_checks_pass_including_restart_recovery(harness):
    demo, report, _ = harness
    assert report["daemon_starts"] == 2  # started, SIGKILLed while parked, restarted
    assert all(report["checks"].values()) and len(report["checks"]) >= 8
    for expected in ("parked run survives a hard kill and keeps its task parked", "operator token cannot decide",
                     "undecided approval expires and the sweep fails the gated task", "live backup verifies"):
        assert report["checks"][expected]


def test_timeline_covers_decision_denial_and_expiry(harness):
    _, report, _ = harness
    text = "\n".join(report["timeline"])
    assert "approval.decide (denied)" in text and "human:local-approver approval.decide (allowed)" in text
    assert "daemon:expiry approval.expire (allowed)" in text
    assert "requested -> expired" in text and "running -> failed" in text and "running -> succeeded" in text


def test_no_token_appears_in_outputs_logs_database_or_backup(harness):
    demo, report, state = harness
    tokens = [(state / "operator.token").read_bytes(), demo.approver.encode()]
    assert all(len(t) == 43 for t in tokens) and tokens[0] != tokens[1]
    blobs = ["\n".join(demo.outputs + report["timeline"]).encode(), demo.log_path.read_bytes(),
             (state.parent / "state-backup.sqlite").read_bytes()]
    blobs += [p.read_bytes() for p in state.glob("synapse.sqlite*") if p.is_file()]
    for blob in blobs:
        for token in tokens:
            assert token not in blob
    with sqlite3.connect(state / "synapse.sqlite") as db:
        for table in [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            for row in db.execute(f'SELECT * FROM "{table}"'):
                text = repr(row)
                assert all(t.decode() not in text for t in tokens)
