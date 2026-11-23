"""Shared fixtures for the fault-injection suite (see docs/operations/fault-injection.md)."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil

from synapse.daemon.mock_runtime import MockRuntime
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


class World:
    """A migrated database, a private blob directory and a MockRuntime over a two-node workflow."""

    def __init__(self, tmp_path: Path, nodes=(("build", ()), ("review", ("build",)))):
        self.dir = Path(tmp_path)
        self.path = self.dir / "db"
        self.blobs = self.dir / "blobs"
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        self.db = open_database(self.path)
        apply_migrations(self.db, MIGRATIONS)
        publish_workflow(self.db, workflow_id="wf", version="1",
                         nodes=[{"id": n, "dependencies": list(d)} for n, d in nodes],
                         principal="operator", idempotency_key="publish")
        self.run_id = create_run(self.db, workflow_id="wf", version="1", principal="operator",
                                 idempotency_key="create").response["run_id"]
        self.runtime = self.new_runtime()

    def new_runtime(self) -> MockRuntime:
        runtime = MockRuntime(self.db, self.blobs)
        runtime._park_poll = 0.01
        return runtime

    def restart(self, *, recover: bool = True) -> None:
        """What a crashed-and-restarted daemon does: new connection, new runtime, startup recovery."""
        self.db.close()
        self.db = open_database(self.path)
        self.runtime = self.new_runtime()
        if recover:
            self.runtime.recover_interrupted()

    def states(self):
        run = self.db.execute("SELECT state FROM runs WHERE id=?", (self.run_id,)).fetchone()[0]
        return run, dict(self.db.execute("SELECT node_id,state FROM tasks WHERE run_id=?", (self.run_id,)))

    def attempts(self):
        return self.db.execute("SELECT t.node_id,a.attempt_no,a.state FROM task_attempts a JOIN tasks t ON t.id=a.task_id "
                               "ORDER BY t.node_id,a.attempt_no").fetchall()

    async def until(self, predicate, max_ticks: int = 200_000):
        """Poll once per event-loop turn (the mock engine yields every step, so a state that lasts
        a few turns is observable); bounded by turns, not wall-clock, so it is deterministic."""
        for _ in range(max_ticks):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError("never reached; state=%r attempts=%r" % (self.states(), self.attempts()))

    async def stop(self) -> None:
        """Simulate the daemon process dying mid-flight: cancel the run task, nothing else runs."""
        self.runtime._active.cancel()
        await asyncio.gather(self.runtime._active, return_exceptions=True)

    def snapshot_to(self, target: Path) -> Path:
        """A crash image: byte-for-byte copy of the committed database state (WAL included)."""
        import sqlite3
        dst = sqlite3.connect(target)
        self.db.backup(dst)
        dst.close()
        return target

    def close(self) -> None:
        self.db.close()


class FakeClock:
    """Injectable wall clock for the modules that read ``datetime.now`` (approval store and expiry sweep).

    ``set`` can move time forwards or backwards, which is how clock jumps are injected: the store
    re-reads the clock itself, so a parameter to the sweep alone would not exercise it.
    """

    def __init__(self, monkeypatch, start):
        from datetime import datetime, timezone
        clock = self
        self.now = start.astimezone(timezone.utc)

        class _Datetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz) if tz is not None else clock.now.replace(tzinfo=None)

        for module in ("synapse.persistence.approval_store", "synapse.persistence.approval_expiry"):
            monkeypatch.setattr(module + ".datetime", _Datetime)

    def set(self, value) -> None:
        self.now = value

    def shift(self, delta) -> None:
        self.now = self.now + delta


def copy_world_files(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)
