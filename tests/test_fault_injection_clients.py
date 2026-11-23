"""Client-side fault injection: a dropped client can never cancel work (ARCHITECTURE.md §13, invariant 8).

A real daemon with a run parked on an approval gets abruptly dropped HTTP and WebSocket clients
(no close frame, half-sent request, many in a row). The run must stay running and parked, nothing
may record a cancellation, and a human approval afterwards must still finish it.
"""
import importlib.util
from pathlib import Path
import signal
import socket
import sqlite3

from invariants import check_invariants

ROOT = Path(__file__).resolve().parents[1]


def _load_demo():
    spec = importlib.util.spec_from_file_location("demo_mock_for_client_faults", ROOT / "scripts" / "demo_mock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _half_request(sock_path: Path, token: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall(b"GET /api/v1/runs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + token.encode() + b"\r\n")  # never finished


def _dropped_stream(sock_path: Path, token: str) -> None:
    from websockets.sync.client import unix_connect
    ws = unix_connect(str(sock_path), uri="ws://localhost/api/v1/events/stream",
                      additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=5)
    ws.socket.close()  # abrupt: no close frame, as when a desktop window is killed or the network drops


def _cancellations(db, run_id):
    events = db.execute("SELECT type FROM events WHERE run_id=? AND (type LIKE '%cancel%')", (run_id,)).fetchall()
    audits = db.execute("SELECT action FROM audit_entries WHERE action LIKE '%cancel%'").fetchall()
    return events, audits


def test_dropped_clients_never_cancel_a_parked_run_and_approval_still_finishes_it(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    demo = _load_demo().Demo(state, quiet=True)
    try:
        demo.start_daemon()
        demo.provision()
        demo.publish("demo-gated")
        run_id = demo.queue("demo-gated")
        demo.mock_start(run_id)
        approval = demo.approval_for(run_id)
        demo.wait("review to park", lambda: demo.task_states(run_id).get("review") == "awaiting_approval")
        token = (state / "operator.token").read_text().strip()
        sock = state / "agentd.sock"
        for _ in range(5):
            _half_request(sock, token)
            _dropped_stream(sock, token)
        # The daemon is still serving, and nothing about the run changed.
        assert demo.ctl_json("health")["status"] == "ok"
        assert demo.run_state(run_id) == "running"
        assert demo.task_states(run_id).get("review") == "awaiting_approval"
        with demo.db() as db:
            assert _cancellations(db, run_id) == ([], [])
        demo.decide(approval, "approved", demo.approver)
        demo.wait("gated run to finish", lambda: demo.run_state(run_id) in ("succeeded", "failed"))
        assert demo.run_state(run_id) == "succeeded"
        with demo.db() as db:
            assert _cancellations(db, run_id) == ([], [])
    finally:
        demo.stop_daemon(signal.SIGTERM) if demo.proc and demo.proc.poll() is None else None
        demo.close()
    with sqlite3.connect(f"file:{state / 'synapse.sqlite'}?mode=ro", uri=True) as db:
        violations = check_invariants(db, quiescent=True, blob_dir=state / "artifacts")
        assert violations == [], "\n".join(map(str, violations))
