"""Fault injection for message delivery and event replay: lease expiry, clock jumps, duplicate delivery."""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from invariants import assert_invariants
from synapse.persistence.delivery_store import (
    LeaseConflict, claim_deliveries, complete_delivery_idempotently, finish_delivery,
)
from synapse.persistence.dispatcher import dispatch_batch
from synapse.persistence.event_store import pending_outbox, read_events
from test_message_store import db, send  # noqa: F401  (migrated message fixture)

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def transact(db, action):
    db.execute("BEGIN IMMEDIATE")
    try:
        value = action()
        db.execute("COMMIT")
        return value
    except BaseException:
        db.execute("ROLLBACK")
        raise


def claim(db, now, max_attempts=3):
    return transact(db, lambda: claim_deliveries(db, recipient="receiver", now=now, lease_seconds=10, max_attempts=max_attempts))


def ack(db, lease, now, success=True, max_attempts=3):
    return transact(db, lambda: finish_delivery(
        db, recipient="receiver", delivery_id=lease.delivery_id, token=lease.token, success=success,
        result_artifact_id="artifact" if success else None, max_attempts=max_attempts, now=now))


def test_lease_expiry_redelivers_and_clock_jumps_never_steal_a_live_lease(db):
    send(db)
    assert_invariants(db)
    first, = claim(db, NOW)
    assert_invariants(db)
    assert claim(db, NOW - timedelta(hours=1)) == []  # clock jumped back: the lease is still held
    assert claim(db, NOW + timedelta(seconds=9)) == []  # just inside the lease
    assert_invariants(db)
    second, = claim(db, NOW + timedelta(seconds=11))  # clock moved on: redelivery with a fresh token
    assert second.attempt == 2 and second.token != first.token
    assert_invariants(db)
    with pytest.raises(LeaseConflict):  # the first worker wakes up late and tries to ACK
        ack(db, first, NOW + timedelta(seconds=12))
    assert db.execute("SELECT state,attempts FROM message_deliveries").fetchone() == ("leased", 2)
    assert_invariants(db)
    ack(db, second, NOW + timedelta(seconds=12))
    with pytest.raises(LeaseConflict):  # a duplicate ACK of the same lease changes nothing
        ack(db, second, NOW + timedelta(seconds=13))
    assert db.execute("SELECT state FROM message_deliveries").fetchone() == ("acknowledged",)
    assert claim(db, NOW + timedelta(days=1)) == []  # an acknowledged delivery is never redelivered
    assert_invariants(db)


def test_exhausted_deliveries_dead_letter_instead_of_looping(db):
    send(db)
    claim(db, NOW, max_attempts=2)
    second, = claim(db, NOW + timedelta(seconds=11), max_attempts=2)
    ack(db, second, NOW + timedelta(seconds=12), success=False, max_attempts=2)  # NACK on the last allowed attempt
    assert db.execute("SELECT state,attempts FROM message_deliveries").fetchone() == ("dead_letter", 2)
    assert claim(db, NOW + timedelta(days=1), max_attempts=2) == []
    assert_invariants(db)


def test_duplicate_completion_replays_its_receipt_and_changes_nothing_twice(db):
    send(db)
    lease, = claim(db, NOW)

    def complete():
        return complete_delivery_idempotently(
            db, principal="receiver", idempotency_key="ack-1", recipient="receiver", delivery_id=lease.delivery_id,
            token=lease.token, success=True, result_artifact_id="artifact", now=NOW + timedelta(seconds=1))
    first = complete()
    again = complete()
    assert first.response == again.response and again.replayed and not first.replayed
    assert db.execute("SELECT COUNT(*) FROM events WHERE type='delivery.acknowledged'").fetchone() == (1,)
    assert_invariants(db)


def test_a_publisher_that_crashes_before_the_ack_redelivers_the_same_event_ids(db, monkeypatch):
    """Outbox delivery is at-least-once: a duplicate is the identical event, never a new id."""
    from synapse.persistence import dispatcher
    send(db)
    published = []
    real = dispatcher.record_dispatch_result

    def crash_before_ack(*args, **kwargs):
        raise RuntimeError("process died after publishing, before the ACK was recorded")
    monkeypatch.setattr(dispatcher, "record_dispatch_result", crash_before_ack)
    with pytest.raises(RuntimeError, match="died"):
        dispatch_batch(db, lambda event: published.append(event["id"]), limit=1)
    assert db.execute("SELECT COUNT(*) FROM outbox WHERE dispatched_at IS NOT NULL").fetchone() == (0,)  # nothing was acknowledged
    monkeypatch.setattr(dispatcher, "record_dispatch_result", real)  # the daemon restarted
    dispatch_batch(db, lambda event: published.append(event["id"]))
    assert published.count(published[0]) == 2 and len(set(published)) == len(published) - 1  # one duplicate, same id
    assert pending_outbox(db) == []
    dispatch_batch(db, lambda event: published.append(event["id"]))
    assert len(published) == 1 + len(set(published))  # an acknowledged event is never published again
    assert_invariants(db)


def test_duplicate_event_rows_are_rejected_and_replay_is_stable(db):
    send(db)
    first_page = read_events(db, after_sequence=0, limit=100)
    event_id = db.execute("SELECT id FROM events LIMIT 1").fetchone()[0]
    for statement in (
        "INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) VALUES(?, 'x','x','x',1,'{}','now')",
        "INSERT INTO outbox(event_id) VALUES(?)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement, (event_id,))
    assert read_events(db, after_sequence=0, limit=100) == first_page  # replay is deterministic
    assert_invariants(db)
