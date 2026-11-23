#!/usr/bin/env python3
"""One-command Control Center demo on the MOCK provider (see docs/operations/demo.md).

    uv run --extra desktop --with-editable . python scripts/demo.py

Starts a real daemon subprocess on a throwaway private state directory, seeds a multi-step workflow with a
human approval gate, opens the desktop Control Center on the live daemon, waits for you to approve, then
cleans everything up. No Claude quota, no real provider: every task is the synthetic mock.

The daemon launch, gating patch and CLI helpers are shared with scripts/demo_mock.py. The approver token is
never printed; the GUI copies it to the clipboard (cleared on exit if unchanged) and the printed agentctl
command reads it from the token file with `$(cat ...)`.
"""
from __future__ import annotations

import argparse
import shlex
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_mock as dm  # noqa: E402

PACE = 2.5  # seconds each mock task stays 'running' so the parallel steps are visible
TERMINAL = ("succeeded", "failed", "cancelled")
ORDER = [n["id"] for n in dm.PIPELINE_NODES]


class Session(dm.Demo):
    """demo_mock.Demo plus the pipeline run: seed, start, wait for the gate, decide, report."""

    run_id = ""
    approval: dict | None = None

    def seed(self) -> None:
        self.start_daemon()
        self.provision()
        self.publish(dm.PIPELINE_ID, dm.PIPELINE_NODES)
        self.run_id = self.queue(dm.PIPELINE_ID)
        self.mock_start(self.run_id)

    def snapshot(self) -> str:
        try:
            tasks = self.task_states(self.run_id)
            return f"run={self.run_state(self.run_id):<18} " + " ".join(f"{n}={tasks.get(n, '?')}" for n in ORDER)
        except dm.DemoError:
            return "(daemon not answering)"

    def watch(self, until, timeout: float, label: str) -> None:
        """Print the run states whenever they change until `until()` is true."""
        deadline, last, start = time.monotonic() + timeout, "", time.monotonic()
        while time.monotonic() < deadline:
            line = self.snapshot()
            if line != last:
                self.say(f"  t+{time.monotonic() - start:4.1f}s  {line}")
                last = line
            if until():
                # until() polls separately from snapshot(): the state may have changed in between (slow runner),
                # so print the settled state too, or the transition we waited for never reaches the output.
                line = self.snapshot()
                if line != last:
                    self.say(f"  t+{time.monotonic() - start:4.1f}s  {line}")
                return
            time.sleep(0.25)
        raise dm.DemoError("timed out waiting for " + label)

    def parked(self) -> bool:
        return self.task_states(self.run_id).get("deploy-verify") == "awaiting_approval"

    def pending_approval(self) -> dict:
        self.approval = self.approval_for(self.run_id)
        return self.approval

    def approve_command(self) -> str:
        a = self.approval
        token_file = shlex.quote(str(self.state / "approver.token"))
        args = ["approval-decide", a["id"], "--decision", "approved", "--expected-version", str(a["state_version"]),
                "--operation", a["operation"], "--target", a["target"], "--candidate-sha256", a["candidate_manifest_sha256"],
                "--policy-version", a["policy_version"], "--reason", "demo", "--idempotency-key", "demo-approve-" + a["id"],
                "--correlation-id", "demo"]
        return (f'SYNAPSE_APPROVER_TOKEN="$(cat {token_file})" {shlex.quote(sys.executable)} -m synapse.cli '
                f'--state-dir {shlex.quote(str(self.state))} ' + " ".join(shlex.quote(x) for x in args))

    def approve(self) -> None:
        token = (self.state / "approver.token").read_text().strip()
        self.decide(self.approval, "approved", token)

    def final_report(self) -> bool:
        tasks = self.task_states(self.run_id)
        state = self.run_state(self.run_id)
        self.say("\nFinal state: run " + state)
        for node in ORDER:
            self.say(f"  {node:<14} {tasks.get(node, '?')}")
        return state == "succeeded"


def banner(session: Session, *, gui: bool) -> None:
    where = "click Approve in the Control Center (paste the approver token from the clipboard), or run:" if gui else \
        "headless mode approves for you; to approve by hand instead, run:"
    print("\n" + "=" * 78 + "\nThe run is parked at 'awaiting approval' (step deploy-verify).\n" + where + "\n\n  "
          + session.approve_command() + "\n" + "=" * 78 + "\n", flush=True)


def run_headless(session: Session, args) -> bool:
    session.say("tasks:")
    session.watch(session.parked, 60, "the run to reach the approval gate")
    session.pending_approval()
    banner(session, gui=False)
    if args.no_approve_wait:
        return True
    session.say("approving as the human approver (token read from the state dir, not printed)")
    session.approve()
    session.watch(lambda: session.run_state(session.run_id) in TERMINAL, 60, "the run to finish")
    return session.final_report()


def run_gui(session: Session, args) -> bool:
    import os
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from synapse.desktop.main_window import MainWindow
    from synapse.desktop.theme import apply_theme

    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(session.state)
    window.show()
    window.control_center.run_selected.emit(session.run_id)  # same signal as picking the run in the switcher
    session.say("Control Center opened on the live daemon. Watching the run...")
    token = (session.state / "approver.token").read_text().strip()
    ours = {"copied": False}
    shots = [1]
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    status = {"phase": "wait-gate", "last": "", "done_at": None, "ok": False}

    def tick() -> None:
        line = session.snapshot()
        if line != status["last"]:
            session.say("  " + line)
            status["last"] = line
            if args.screenshots:  # one PNG per observed state change (development/verification aid)
                path = str(args.screenshots / f"{shots[0]:02d}.png")
                shots[0] += 1
                QTimer.singleShot(1500, lambda path=path: window.grab().save(path, "PNG"))  # after the poller has caught up
        if status["phase"] == "wait-gate" and "deploy-verify=awaiting_approval" in line:
            session.pending_approval()
            if not args.no_clipboard:
                app.clipboard().setText(token)
                ours["copied"] = True
            banner(session, gui=True)
            status["phase"] = "gated" if not args.no_approve_wait else "idle"
        elif status["phase"] == "gated" and any(f"run={t}" in line for t in TERMINAL):
            status["ok"] = session.final_report()
            status["phase"] = "linger"
            status["done_at"] = time.monotonic()
            session.say(f"Closing in {args.linger:g}s (close the window to exit now).")
        elif status["phase"] == "linger" and time.monotonic() - status["done_at"] >= args.linger:
            app.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(300)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    try:
        app.exec()
    finally:
        timer.stop()
        if ours["copied"] and app.clipboard().text() == token:
            app.clipboard().clear()
        window.poller.stop_event.set()
        window.poller.wait(5000)
    return status["ok"] or status["phase"] == "idle"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Control Center demo on the mock provider (no quota, no real provider)")
    parser.add_argument("--headless", action="store_true", help="no window: approve through agentctl and print the run states (CI/smoke)")
    parser.add_argument("--keep-state", action="store_true", help="keep the temporary state directory (it holds the tokens; 0700)")
    parser.add_argument("--no-approve-wait", action="store_true", help="do not wait for or perform the approval: stop at the gate")
    parser.add_argument("--no-clipboard", action="store_true", help="GUI: do not copy the approver token to the clipboard")
    parser.add_argument("--linger", type=float, default=6.0, help="GUI: seconds to keep the window after the run finishes (default 6)")
    parser.add_argument("--pace", type=float, default=PACE, help="seconds each mock task stays running (default %(default)s)")
    parser.add_argument("--screenshots", type=Path, metavar="DIR", help="GUI: save a window PNG ~1.5s after every state change into DIR")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="synapse-demo-"))
    state = base / "state"
    state.mkdir(mode=0o700)
    session = Session(state, pace=args.pace)
    ok = False
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        print(f"Starting a real daemon on the MOCK provider (state dir {base}) ...", flush=True)
        session.seed()
        print(f"Workflow '{dm.PIPELINE_ID}' started: run {session.run_id}", flush=True)
        ok = run_headless(session, args) if args.headless else run_gui(session, args)
    except KeyboardInterrupt:
        print("\ninterrupted; cleaning up", file=sys.stderr)
    except dm.DemoError as exc:
        print("demo failed: " + str(exc), file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, previous)
        session.close()
        if args.keep_state:
            print(f"state kept in {base} (daemon stopped; tokens inside, mode 0700)", flush=True)
        else:
            shutil.rmtree(base, ignore_errors=True)
            print("daemon stopped, temporary state removed", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
