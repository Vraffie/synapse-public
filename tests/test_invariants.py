"""The invariant checker itself: a healthy database passes, every invariant detects its corruption."""
import asyncio
from contextlib import closing
import shutil
import sqlite3

import pytest

from fault_helpers import World
from invariants import REQUIRED_TRIGGERS, assert_invariants, check_invariants
from synapse.persistence.migrations import open_database


def ids(violations):
    return {v.invariant for v in violations}


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """A finished run with a retried attempt (build: fail -> succeed), verified artifacts and blobs."""
    world = World(tmp_path_factory.mktemp("baseline"))

    async def go():
        world.runtime.start(world.run_id, "start", {"build": ["fail", "succeed"], "review": "succeed"})
        await world.runtime._active
    asyncio.run(go())
    assert world.states()[0] == "succeeded"
    assert [a for a in world.attempts() if a[0] == "build"] == [("build", 1, "failed"), ("build", 2, "completed")]
    yield world
    world.close()


@pytest.fixture
def broken(baseline, tmp_path):
    """A writable copy of the healthy database (foreign keys off, so corruption can be planted)."""
    target = tmp_path / "copy.db"
    baseline.snapshot_to(target)
    shutil.copytree(baseline.blobs, tmp_path / "blobs")
    class Corruptible:
        """The connection plus the path of its blob directory (sqlite3.Connection takes no attributes)."""
        def __init__(self, db, blobs):
            self._db, self.blobs = db, blobs

        def __getattr__(self, name):
            return getattr(self._db, name)

    with closing(open_database(target)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        yield Corruptible(db, tmp_path / "blobs")


def drop(db, *triggers):
    for name in triggers:
        db.execute(f"DROP TRIGGER {name}")


def first(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def test_a_healthy_database_has_no_violations(baseline):
    assert check_invariants(baseline.db, quiescent=True, blob_dir=baseline.blobs) == []
    assert check_invariants(baseline.db) == []


def test_check_is_read_only_and_leaves_no_open_transaction(baseline):
    before = baseline.db.execute("SELECT COUNT(*) FROM events").fetchone()
    check_invariants(baseline.db)
    assert not baseline.db.in_transaction and baseline.db.execute("SELECT COUNT(*) FROM events").fetchone() == before


def test_assert_invariants_reports_every_violation(broken):
    drop(broken, "messages_prevent_update")
    with pytest.raises(AssertionError, match="schema.immutability"):
        assert_invariants(broken)


# ------------------------------------------------------------------ schema
def test_detects_missing_immutability_trigger(broken):
    drop(broken, "events_prevent_update")
    assert "schema.immutability" in ids(check_invariants(broken))


def test_every_required_trigger_exists_on_a_healthy_database(baseline):
    present = {r[0] for r in baseline.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert set(REQUIRED_TRIGGERS) <= present  # a renamed trigger must fail here, not silently pass


def test_detects_noncontiguous_migrations(broken):
    broken.execute("DELETE FROM schema_migrations WHERE version=2")
    assert "schema.migrations" in ids(check_invariants(broken))


def test_detects_foreign_key_violation(broken):
    broken.execute("UPDATE tasks SET run_id='ghost' WHERE id=(SELECT id FROM tasks LIMIT 1)")
    assert "schema.foreign_keys" in ids(check_invariants(broken))


# ------------------------------------------------------------------ events / outbox
def test_detects_event_without_outbox_row(broken):
    broken.execute("DELETE FROM outbox WHERE event_id=(SELECT id FROM events ORDER BY sequence DESC LIMIT 1)")
    assert "events.outbox" in ids(check_invariants(broken))


def test_detects_outbox_row_without_event(broken):
    drop(broken, "events_prevent_delete")
    last = first(broken, "SELECT id FROM events ORDER BY sequence DESC LIMIT 1")
    broken.execute("DELETE FROM events WHERE id=?", (last,))
    assert "events.outbox" in ids(check_invariants(broken))


def test_detects_event_sequence_gap(broken):
    drop(broken, "events_prevent_delete")
    middle = first(broken, "SELECT id FROM events ORDER BY sequence LIMIT 1 OFFSET 3")
    broken.execute("DELETE FROM outbox WHERE event_id=?", (middle,))
    broken.execute("DELETE FROM events WHERE id=?", (middle,))
    assert "events.sequence" in ids(check_invariants(broken))


def test_detects_non_object_event_payload(broken):
    drop(broken, "events_prevent_update")
    broken.execute("UPDATE events SET payload_json='[1]' WHERE sequence=1")
    assert "events.payload" in ids(check_invariants(broken))


# ------------------------------------------------------------------ run / task history
def test_detects_run_state_without_matching_history(broken):
    broken.execute("UPDATE runs SET state='running'")
    assert "run.history" in ids(check_invariants(broken))


def test_detects_run_version_without_event(broken):
    broken.execute("UPDATE runs SET state_version=state_version+1")
    assert "run.history" in ids(check_invariants(broken))


def test_detects_run_leaving_a_terminal_state(broken):
    drop(broken, "events_prevent_update")
    broken.execute("UPDATE events SET payload_json=json_set(payload_json,'$.before','succeeded') "
                   "WHERE type='run.state_changed' AND json_extract(payload_json,'$.after')='succeeded'")
    assert "run.terminal" in ids(check_invariants(broken))


def test_detects_task_state_without_matching_history(broken):
    broken.execute("UPDATE tasks SET state='ready' WHERE node_id='build'")
    assert "task.history" in ids(check_invariants(broken))


def test_detects_task_leaving_a_terminal_state(broken):
    drop(broken, "events_prevent_update")
    broken.execute("UPDATE events SET payload_json=json_set(payload_json,'$.before','completed') WHERE rowid=("
                   "SELECT rowid FROM events WHERE type='task.state_changed' AND json_extract(payload_json,'$.after')='completed' LIMIT 1)")
    assert "task.terminal" in ids(check_invariants(broken))


def test_detects_untransitioned_task_that_is_not_created(broken):
    drop(broken, "events_prevent_delete")
    task = first(broken, "SELECT id FROM tasks WHERE node_id='review'")
    rows = broken.execute("SELECT id FROM events WHERE aggregate_id=? AND type IN ('task.state_changed','task.attempt_reserved')", (task,)).fetchall()
    for (event_id,) in rows:
        broken.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
        broken.execute("DELETE FROM events WHERE id=?", (event_id,))
    broken.execute("UPDATE tasks SET state_version=0")
    assert "task.history" in ids(check_invariants(broken))


# ------------------------------------------------------------------ attempts
def test_detects_live_attempt_in_a_finished_run(broken):
    broken.execute("UPDATE task_attempts SET state='running' WHERE id=(SELECT id FROM task_attempts LIMIT 1)")
    found = ids(check_invariants(broken))
    assert "attempt.live" in found and "attempt.history" in found


def test_quiescent_mode_flags_any_live_attempt_even_in_a_running_run(broken):
    broken.execute("UPDATE runs SET state='running'")
    broken.execute("UPDATE tasks SET state='running' WHERE node_id='review'")
    broken.execute("UPDATE task_attempts SET state='running' WHERE task_id=(SELECT id FROM tasks WHERE node_id='review')")
    assert "attempt.live" not in ids(check_invariants(broken))  # a running run may have a live worker
    assert "attempt.live" in ids(check_invariants(broken, quiescent=True))


def test_quiescent_mode_flags_task_in_flight_of_a_stopped_run(broken):
    broken.execute("UPDATE tasks SET state='running' WHERE node_id='review'")
    assert "task.quiescent" in ids(check_invariants(broken, quiescent=True))


def test_detects_attempt_without_a_recorded_attempt_count(broken):
    broken.execute("UPDATE tasks SET attempt_count=attempt_count-1 WHERE node_id='build'")
    assert "attempt.count" in ids(check_invariants(broken))


def test_detects_phantom_respawn_attempt(broken):
    task = first(broken, "SELECT id FROM tasks WHERE node_id='build'")
    broken.execute("INSERT INTO task_attempts(id,task_id,attempt_no,state) VALUES('phantom',?,3,'unknown')", (task,))
    assert "attempt.count" in ids(check_invariants(broken))


def test_detects_two_unresolved_attempts_for_one_task(broken):
    task = first(broken, "SELECT id FROM tasks WHERE node_id='build'")
    broken.execute("UPDATE task_attempts SET state='unknown' WHERE task_id=?", (task,))
    assert "attempt.unresolved" in ids(check_invariants(broken))


def test_detects_attempt_leaving_a_terminal_state(broken):
    drop(broken, "events_prevent_update")
    broken.execute("UPDATE events SET payload_json=json_set(payload_json,'$.before','completed') WHERE rowid=("
                   "SELECT rowid FROM events WHERE type='task_attempt.state_changed' AND json_extract(payload_json,'$.after')='failed' LIMIT 1)")
    assert "attempt.terminal" in ids(check_invariants(broken))


# ------------------------------------------------------------------ deliveries
def _delivery(db, state, *, owner=None, until=None, acked=None, attempts=1):
    db.execute("INSERT INTO workflow_versions SELECT 'wf2','1','{}','h','now' WHERE NOT EXISTS (SELECT 1 FROM workflow_versions WHERE workflow_id='wf2')")
    db.execute("INSERT INTO message_deliveries(id,message_id,recipient_instance_id,state,attempts,lease_owner,lease_until,acknowledged_at) "
               "VALUES('d','m','r',?,?,?,?,?)", (state, attempts, owner, until, acked))


@pytest.mark.parametrize("kwargs", [
    {"state": "leased"},                                           # leased without owner/expiry
    {"state": "pending", "owner": "tok", "until": "t"},            # lease on a non-leased row
    {"state": "acknowledged", "acked": None},                      # ACK without timestamp
    {"state": "pending", "acked": "t"},                            # acknowledged but still pending
    {"state": "bogus"},                                            # unknown state
    {"state": "leased", "owner": "o", "until": "t", "attempts": 0},  # leased, never attempted
    {"state": "pending", "attempts": -1},
], ids=["leased-no-lease", "pending-with-lease", "ack-no-timestamp", "pending-acked", "unknown-state", "leased-zero-attempts", "negative-attempts"])
def test_detects_inconsistent_delivery(broken, kwargs):
    _delivery(broken, **kwargs)
    assert "delivery.state" in ids(check_invariants(broken))


def test_accepts_consistent_deliveries(broken):
    _delivery(broken, "leased", owner="tok", until="2099", attempts=1)
    assert "delivery.state" not in ids(check_invariants(broken))


# ------------------------------------------------------------------ approvals
def _approval(db, state="requested", **kw):
    row = {"id": "ap", "run_id": first(db, "SELECT id FROM runs"), "operation": "op", "target": "t",
           "candidate_manifest_sha256": "c", "policy_version": "1", "state": state,
           "expires_at": "2030-01-01T00:00:00+00:00", "requested_at": "2029-12-01T00:00:00+00:00",
           "decided_at": None, "decision_principal": None, "requester_subject": None, "requester_kind": None}
    row.update(kw)
    db.execute(f"INSERT INTO approval_requests({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


def test_detects_decision_without_decider(broken):
    _approval(broken, "approved")
    assert "approval.decision" in ids(check_invariants(broken))


def test_detects_decision_on_a_requested_approval(broken):
    _approval(broken, "requested", decided_at="2029-12-02T00:00:00+00:00", decision_principal="human")
    assert "approval.decision" in ids(check_invariants(broken))


def test_detects_self_approval(broken):
    _approval(broken, "approved", decided_at="2029-12-02T00:00:00+00:00", decision_principal="agent-1",
              requester_subject="agent-1", requester_kind="agent")
    assert "approval.self" in ids(check_invariants(broken))


def test_detects_approval_after_expiry(broken):
    _approval(broken, "approved", decided_at="2031-01-01T00:00:00+00:00", decision_principal="human")
    assert "approval.expiry" in ids(check_invariants(broken))


def test_accepts_a_proper_approval(broken):
    _approval(broken, "approved", decided_at="2029-12-02T00:00:00+00:00", decision_principal="human",
              requester_subject="agent-1", requester_kind="agent")
    assert not {i for i in ids(check_invariants(broken)) if i.startswith("approval.")}


def test_detects_awaiting_task_without_gate(broken):
    broken.execute("UPDATE tasks SET state='awaiting_approval' WHERE node_id='review'")
    assert "approval.gate" in ids(check_invariants(broken))


def test_detects_unresolved_gate_on_a_task_that_is_not_waiting(broken):
    _approval(broken, "requested")
    task = first(broken, "SELECT id FROM tasks WHERE node_id='review'")
    drop(broken, "task_gate_insert_unresolved", "task_gate_valid")
    broken.execute("INSERT INTO task_approval_gates(task_id,approval_id,run_id,created_at) VALUES(?, 'ap', (SELECT run_id FROM tasks WHERE id=?), 'now')", (task, task))
    assert "approval.gate" in ids(check_invariants(broken))


# ------------------------------------------------------------------ verification
def test_detects_verification_without_candidate_artifact(broken):
    drop(broken, "verification_results_no_update")
    broken.execute("UPDATE verification_results SET candidate_artifact_id=NULL WHERE rowid=(SELECT MIN(rowid) FROM verification_results)")
    assert "verification.artifact" in ids(check_invariants(broken))


def test_detects_verification_with_different_candidate_hash(broken):
    drop(broken, "verification_results_no_update")
    broken.execute("UPDATE verification_results SET candidate_sha256=? WHERE rowid=(SELECT MIN(rowid) FROM verification_results)", ("0" * 64,))
    assert "verification.artifact" in ids(check_invariants(broken))


def test_detects_verification_not_bound_to_an_attempt_of_its_task(broken):
    drop(broken, "verification_results_no_update")
    broken.execute("UPDATE verification_results SET task_attempt_id=NULL WHERE rowid=(SELECT MIN(rowid) FROM verification_results)")
    assert "verification.attempt" in ids(check_invariants(broken))


def test_detects_approval_of_an_attempt_that_did_not_complete(broken):
    broken.execute("UPDATE task_attempts SET state='failed' WHERE id=(SELECT task_attempt_id FROM verification_results WHERE outcome='approved' LIMIT 1)")
    assert "verification.attempt" in ids(check_invariants(broken))


def test_detects_completed_task_without_approved_verification(broken):
    drop(broken, "verification_results_no_update")
    broken.execute("UPDATE verification_results SET outcome='rejected'")
    assert "verification.completed" in ids(check_invariants(broken))


# ------------------------------------------------------------------ usage / artifacts
def test_detects_usage_row_that_reports_nothing(broken):
    attempt = first(broken, "SELECT id FROM task_attempts LIMIT 1")
    broken.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) SELECT 'p','mock','P','p','now' WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE id='p')")
    broken.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,confidence,observed_at) VALUES('u',?, 'p','x','reported','now')", (attempt,))
    assert "usage.unknown" in ids(check_invariants(broken))


def test_a_real_usage_row_is_fine(broken):
    attempt = first(broken, "SELECT id FROM task_attempts LIMIT 1")
    broken.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) SELECT 'p','mock','P','p','now' WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE id='p')")
    broken.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,confidence,observed_at) VALUES('u',?, 'p','x',0,'reported','now')", (attempt,))
    assert "usage.unknown" not in ids(check_invariants(broken))


def test_detects_artifact_metadata_without_blob(broken):
    blob = next(p for p in broken.blobs.rglob("*") if p.is_file())
    blob.unlink()
    assert "artifact.blob" in ids(check_invariants(broken, blob_dir=broken.blobs))


def test_detects_artifact_blob_with_wrong_size(broken):
    drop(broken, "artifacts_prevent_update")
    broken.execute("UPDATE artifacts SET byte_size=byte_size+1 WHERE rowid=(SELECT MIN(rowid) FROM artifacts)")
    assert "artifact.blob" in ids(check_invariants(broken, blob_dir=broken.blobs))


def test_an_orphan_blob_without_metadata_is_allowed(broken):
    """A crash between blob write and metadata commit leaves an unreferenced blob (documented, reconciled later)."""
    from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
    ArtifactBlobStore(broken.blobs, max_bytes=100).put([b"orphan"])
    assert check_invariants(broken, blob_dir=broken.blobs) == []
