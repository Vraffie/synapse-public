"""Fault injection around approval gates: down-time decisions, expiry, clock jumps (ARCHITECTURE.md §13)."""
import asyncio
from datetime import datetime, timedelta
import os

import pytest

from fault_fixtures import starts  # noqa: F401  (fixture)
from fault_helpers import FakeClock, World
from invariants import assert_invariants
from synapse.application.approval_service import ApprovalService
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.persistence.approval_expiry import expire_due_approvals
from synapse.persistence.state_store import StateConflict

DIGEST = "b" * 64
STEPS = {"review": {"operation": "push", "target": "repo", "candidate_sha256": DIGEST, "policy_version": "1"}}


class Gated:
    """A World whose 'review' node waits for a human approval, plus the approver authority."""

    def __init__(self, tmp_path):
        self.world = World(tmp_path)
        directory = tmp_path / "approver"
        directory.mkdir(mode=0o700)
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.token = provision_approver_token(self.fd)
        self.authority = LocalApproverAuthority(self.fd)

    def decide(self, decision="approved", **binding):
        db = self.world.db
        aid = db.execute("SELECT id FROM approval_requests").fetchone()[0]
        spec = dict(operation="push", target="repo", candidate_sha256=DIGEST, policy_version="1")
        spec.update(binding)
        return ApprovalService(db, self.authority).decide(
            self.token, approval_id=aid, expected_version=0, decision=decision, **spec,
            idempotency_key="d-%s-%s" % (decision, "-".join(sorted(binding)) or "exact"), correlation_id="t")

    async def park(self):
        self.world.runtime.start(self.world.run_id, "start", approval_steps=STEPS)
        await self.world.until(lambda: self.world.states()[1].get("review") == "awaiting_approval")

    def close(self):
        self.authority.close()
        os.close(self.fd)
        self.world.close()


@pytest.fixture
def gated(tmp_path):
    g = Gated(tmp_path)
    yield g
    g.close()


def test_parked_approval_survives_a_crash_and_expires_only_forward_in_time(gated, starts, monkeypatch):
    world = gated.world

    async def first():
        await gated.park()
        assert_invariants(world.db)
        await world.stop()
    asyncio.run(first())
    world.restart()
    assert_invariants(world.db, quiescent=True)
    requested, expires = (datetime.fromisoformat(v) for v in world.db.execute(
        "SELECT requested_at,expires_at FROM approval_requests").fetchone())
    clock = FakeClock(monkeypatch, requested)
    clock.set(requested - timedelta(days=1))  # the clock jumped backwards: nothing expires
    assert expire_due_approvals(world.db) == []
    clock.set(expires - timedelta(seconds=1))  # still inside the window
    assert expire_due_approvals(world.db) == []
    assert world.states()[1]["review"] == "awaiting_approval"
    assert_invariants(world.db, quiescent=True)
    clock.set(expires + timedelta(seconds=1))  # forwards past the expiry
    with pytest.raises(StateConflict):  # a late approval is refused even though nobody swept yet
        gated.decide("approved")
    assert world.db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    assert_invariants(world.db, quiescent=True)
    assert len(expire_due_approvals(world.db)) == 1
    assert world.states()[1]["review"] == "failed"
    assert_invariants(world.db, quiescent=True)
    clock.set(requested - timedelta(days=30))  # jumping back again resurrects nothing
    assert expire_due_approvals(world.db) == []
    assert world.db.execute("SELECT state FROM approval_requests").fetchone() == ("expired",)
    assert world.states()[1]["review"] == "failed"
    assert_invariants(world.db, quiescent=True)

    async def second():
        assert world.runtime.resume_parked()
        await asyncio.wait_for(world.runtime._active, 5)
    asyncio.run(second())
    assert world.states() == ("failed", {"build": "completed", "review": "failed"})
    assert len(starts) == 1  # only 'build' ever launched: the expired gate never ran 'review'
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


def test_decision_made_while_the_daemon_was_down_resumes_the_gate_once(gated, starts):
    world = gated.world

    async def first():
        await gated.park()
        await world.stop()
    asyncio.run(first())
    gated.decide("approved")  # a human approves while the daemon is dead
    world.restart()
    assert_invariants(world.db, quiescent=True)

    async def second():
        assert world.runtime.resume_parked()
        await asyncio.wait_for(world.runtime._active, 5)
    asyncio.run(second())
    assert world.states() == ("succeeded", {"build": "completed", "review": "completed"})
    assert len(starts) == 2  # build once, review once
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)
    world.restart()
    assert not world.runtime.resume_parked() and len(starts) == 2  # a finished run is never re-driven
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


def test_rejected_approval_never_launches_the_gated_task(gated, starts):
    world = gated.world

    async def go():
        await gated.park()
        gated.decide("rejected")
        await asyncio.wait_for(world.runtime._active, 5)
    asyncio.run(go())
    assert world.states() == ("failed", {"build": "completed", "review": "failed"})
    assert len(starts) == 1
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


@pytest.mark.parametrize("changed", [{"candidate_sha256": "c" * 64}, {"policy_version": "2"}, {"target": "other-repo"}, {"operation": "deploy"}],
                         ids=["candidate", "policy", "target", "operation"])
def test_a_decision_bound_to_a_changed_candidate_or_policy_is_refused_and_launches_nothing(gated, starts, changed):
    world = gated.world

    async def go():
        await gated.park()
        with pytest.raises(StateConflict, match="binding differs"):
            gated.decide("approved", **changed)
        assert world.db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
        assert world.states()[1]["review"] == "awaiting_approval"  # still parked: nothing was approved
        assert_invariants(world.db)
        await world.stop()
    asyncio.run(go())
    assert len(starts) == 1  # only 'build' ran
    assert_invariants(world.db, quiescent=True)
