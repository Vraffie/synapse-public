"""Smoke test: scripts/demo_agent.py (fake CLI executable) against a REAL daemon subprocess."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("demo_agent", ROOT / "scripts" / "demo_agent.py")
demo_agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo_agent)


def test_fake_cli_agent_flow_start_show_gate_cancel_and_no_leaks(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNAPSE_REAL_CLAUDE", raising=False)
    demo = demo_agent.AgentDemo(tmp_path, real=False, quiet=True)
    try:
        report = demo.run_all()
    finally:
        demo.close()
    assert report["mode"] == "fake" and len(report["checks"]) >= 9 and all(report["checks"].values())
    assert report["plain"]["outcome"] == "succeeded" and report["gated"]["run_state"] == "succeeded"
