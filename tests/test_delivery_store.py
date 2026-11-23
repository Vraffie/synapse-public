from contextlib import closing
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from synapse.persistence.delivery_store import LeaseConflict, claim_deliveries, finish_delivery
from synapse.persistence.migrations import open_database
from test_message_store import db, send  # Reuse the full migrated message fixture.


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


def claim(db, now=NOW, recipient="receiver", max_attempts=3):
    return transact(db, lambda: claim_deliveries(
        db, recipient=recipient, now=now, lease_seconds=10, max_attempts=max_attempts,
    ))


def test_expired_lease_redelivery_rejects_stale_token(db):
    delivery_id = send(db).response[0]
    first, = claim(db)
    assert first.delivery_id == delivery_id
    assert claim(db) == []
    second, = claim(db, NOW + timedelta(seconds=11))
    assert second.token != first.token and second.attempt == 2
    with pytest.raises(LeaseConflict):
        transact(db, lambda: finish_delivery(
            db, recipient="receiver", delivery_id=delivery_id, token=first.token,
            success=True, result_artifact_id="artifact", now=NOW + timedelta(seconds=11),
        ))
    transact(db, lambda: finish_delivery(
        db, recipient="receiver", delivery_id=delivery_id, token=second.token,
        success=True, result_artifact_id="artifact", now=NOW + timedelta(seconds=12),
    ))
    assert db.execute("SELECT state,result_artifact_id FROM message_deliveries").fetchone() == ("acknowledged", "artifact")
    assert len(claim(db, NOW + timedelta(seconds=30))) == 0


def test_nack_retries_and_exhausted_lease_dead_letters(db):
    send(db)
    first, = claim(db, max_attempts=2)
    transact(db, lambda: finish_delivery(
        db, recipient="receiver", delivery_id=first.delivery_id, token=first.token,
        success=False, max_attempts=2, now=NOW,
    ))
    second, = claim(db, NOW + timedelta(seconds=1), max_attempts=2)
    assert second.attempt == 2
    assert claim(db, NOW + timedelta(seconds=12), max_attempts=2) == []
    assert db.execute("SELECT state,attempts FROM message_deliveries").fetchone() == ("dead_letter", 2)


def test_concurrent_claims_yield_one_lease(db):
    send(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)

    def run(_):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            return claim(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(map(len, results)) == [0, 1]


def test_failed_outbox_write_rolls_back_claim(db):
    send(db)
    db.execute("CREATE TRIGGER fail_delivery_event BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'forced'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        claim(db)
    assert db.execute("SELECT state,attempts FROM message_deliveries").fetchone() == ("pending", 0)



def test_restart_recovers_expired_lease_and_rejects_other_recipient(db):
    send(db)
    first, = claim(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(open_database(path)) as reopened:
        assert claim(reopened, NOW + timedelta(seconds=11), recipient="foreign") == []
        recovered, = claim(reopened, NOW + timedelta(seconds=11))
        assert recovered.delivery_id == first.delivery_id
        with pytest.raises(LeaseConflict):
            transact(reopened, lambda: finish_delivery(
                reopened, recipient="foreign", delivery_id=recovered.delivery_id,
                token=recovered.token, success=False, now=NOW + timedelta(seconds=12),
            ))
        with pytest.raises(ValueError, match="does not exist"):
            transact(reopened, lambda: finish_delivery(
                reopened, recipient="receiver", delivery_id=recovered.delivery_id,
                token=recovered.token, success=True, result_artifact_id="missing",
                now=NOW + timedelta(seconds=12),
            ))
        assert reopened.execute("SELECT state FROM message_deliveries").fetchone() == ("leased",)



def test_claim_order_follows_message_insertion_not_timestamp_text(db):
    from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
    from synapse.persistence.message_store import write_message

    first = send(db).response[0]
    second_message = MessageEnvelope(
        id=MessageId("second"), run_id=RunId("run"), thread_id="thread",
        sender_instance_id="sender", type=MessageType.QUESTION, payload={},
        created_at="2026-11-19T09:00:00+00:00",
    )
    second, = transact(db, lambda: write_message(db, second_message, recipients=("receiver",)))
    first_lease, = transact(db, lambda: claim_deliveries(
        db, recipient="receiver", now=NOW, limit=1,
    ))
    assert first_lease.delivery_id == first
    second_lease, = transact(db, lambda: claim_deliveries(
        db, recipient="receiver", now=NOW, limit=1,
    ))
    assert second_lease.delivery_id == second


def test_nack_at_retry_limit_dead_letters_and_rejects_replay(db):
    send(db)
    lease, = claim(db, max_attempts=1)
    transact(db, lambda: finish_delivery(
        db, recipient="receiver", delivery_id=lease.delivery_id, token=lease.token,
        success=False, max_attempts=1, now=NOW,
    ))
    assert db.execute("SELECT state FROM message_deliveries").fetchone() == ("dead_letter",)
    with pytest.raises(LeaseConflict):
        transact(db, lambda: finish_delivery(
            db, recipient="receiver", delivery_id=lease.delivery_id, token=lease.token,
            success=False, max_attempts=1, now=NOW,
        ))


def test_exhausted_delivery_does_not_hide_eligible_work(db):
    from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
    from synapse.persistence.message_store import write_message

    send(db)
    claim(db, max_attempts=1)
    message = MessageEnvelope(
        id=MessageId("second"), run_id=RunId("run"), thread_id="thread",
        sender_instance_id="sender", type=MessageType.QUESTION, payload={},
        created_at="2026-11-19T09:00:00+00:00",
    )
    second, = transact(db, lambda: write_message(db, message, recipients=("receiver",)))
    leases = transact(db, lambda: claim_deliveries(
        db, recipient="receiver", now=NOW + timedelta(seconds=11),
        limit=1, max_attempts=1,
    ))
    assert [lease.delivery_id for lease in leases] == [second]
    assert db.execute("SELECT COUNT(*) FROM message_deliveries WHERE state='dead_letter'").fetchone() == (1,)


def test_ack_event_failure_preserves_lease(db):
    send(db)
    lease, = claim(db)
    db.execute("CREATE TRIGGER fail_ack_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'forced'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        transact(db, lambda: finish_delivery(
            db, recipient="receiver", delivery_id=lease.delivery_id, token=lease.token,
            success=True, result_artifact_id="artifact", now=NOW,
        ))
    assert db.execute("SELECT state,lease_owner FROM message_deliveries").fetchone() == ("leased", lease.token)


def test_lost_ack_response_replays_after_restart_without_second_event(db):
    from synapse.persistence.delivery_store import complete_delivery_idempotently
    from synapse.persistence.idempotency import IdempotencyConflict

    send(db)
    lease, = claim(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]

    def complete(conn, artifact="artifact", now=NOW):
        return complete_delivery_idempotently(
            conn, principal="receiver", idempotency_key="ack-1",
            recipient="receiver", delivery_id=lease.delivery_id, token=lease.token,
            success=True, result_artifact_id=artifact, now=now,
        )

    first = complete(db)
    assert not first.replayed
    event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with closing(open_database(path)) as reopened:
        replay = complete(reopened, now=NOW + timedelta(days=1))
        assert replay.replayed and replay.response == first.response
        assert reopened.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
        with pytest.raises(IdempotencyConflict):
            complete(reopened, artifact="different")


def test_idempotent_ack_failure_does_not_store_response(db):
    from synapse.persistence.delivery_store import complete_delivery_idempotently

    send(db)
    lease, = claim(db)
    with pytest.raises(ValueError, match="does not exist"):
        complete_delivery_idempotently(
            db, principal="receiver", idempotency_key="bad",
            recipient="receiver", delivery_id=lease.delivery_id, token=lease.token,
            success=True, result_artifact_id="missing", now=NOW,
        )
    assert db.execute("SELECT state FROM message_deliveries").fetchone() == ("leased",)
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE operation='delivery.complete'").fetchone() == (0,)




def test_nack_replay_does_not_modify_new_lease(db):
    from synapse.persistence.delivery_store import complete_delivery_idempotently

    send(db)
    first, = claim(db)
    kwargs = dict(principal="receiver", idempotency_key="nack-1", recipient="receiver",
                  delivery_id=first.delivery_id, token=first.token, success=False)
    result = complete_delivery_idempotently(db, **kwargs, now=NOW)
    assert result.response["state"] == "pending" and not result.replayed
    second, = claim(db, NOW + timedelta(seconds=1))
    replay = complete_delivery_idempotently(db, **kwargs, now=NOW + timedelta(seconds=2))
    assert replay.replayed and replay.response == result.response
    assert db.execute("SELECT state,lease_owner FROM message_deliveries").fetchone() == ("leased", second.token)


def test_concurrent_idempotent_completion_commits_one_event(db):
    from synapse.persistence.delivery_store import complete_delivery_idempotently

    send(db)
    lease, = claim(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def complete(_):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            return complete_delivery_idempotently(
                conn, principal="receiver", idempotency_key="ack-race",
                recipient="receiver", delivery_id=lease.delivery_id, token=lease.token,
                success=True, result_artifact_id="artifact", now=NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, range(2)))
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].response == results[1].response
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1


def test_idempotent_claim_replays_same_live_leases_and_rejects_expired(db):
    from synapse.persistence.delivery_store import claim_deliveries_idempotently
    from synapse.persistence.idempotency import IdempotencyConflict

    send(db)
    kwargs = dict(principal="receiver-session", idempotency_key="poll-1", recipient="receiver",
                  lease_seconds=10, max_attempts=3)
    first = claim_deliveries_idempotently(db, **kwargs, now=NOW)
    assert not first.replayed and len(first.response) == 1
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    retry = claim_deliveries_idempotently(db, **kwargs, now=NOW + timedelta(seconds=2))
    assert retry.replayed and retry.response == first.response
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
    with pytest.raises(IdempotencyConflict):
        claim_deliveries_idempotently(db, **{**kwargs, "limit": 1}, now=NOW)
    with pytest.raises(LeaseConflict):
        claim_deliveries_idempotently(db, **kwargs, now=NOW + timedelta(seconds=10))
    second = claim_deliveries_idempotently(db, **{**kwargs, "idempotency_key": "poll-2"},
                                          now=NOW + timedelta(seconds=11))
    assert second.response[0]["attempt"] == 2
    assert second.response[0]["token"] != first.response[0]["token"]
    with pytest.raises(LeaseConflict):
        claim_deliveries_idempotently(db, **kwargs, now=NOW + timedelta(seconds=12))


def test_empty_poll_replay_and_lost_claim_outbox_rollback(db):
    from synapse.persistence.delivery_store import claim_deliveries_idempotently

    kwargs = dict(principal="session", idempotency_key="empty", recipient="receiver", now=NOW)
    assert claim_deliveries_idempotently(db, **kwargs).response == []
    send(db)
    assert claim_deliveries_idempotently(db, **kwargs).response == []
    db.execute("CREATE TRIGGER fail_claim_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'forced'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        claim_deliveries_idempotently(db, **{**kwargs, 'idempotency_key': 'next'})
    assert db.execute("SELECT state,attempts FROM message_deliveries").fetchone() == ('pending', 0)
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE key='next'").fetchone() == (0,)


def test_idempotent_claim_replay_after_ack_fails(db):
    from synapse.persistence.delivery_store import claim_deliveries_idempotently

    send(db)
    kwargs = dict(principal="session", idempotency_key="poll", recipient="receiver", now=NOW)
    first = claim_deliveries_idempotently(db, **kwargs)
    lease, = first.response
    transact(db, lambda: finish_delivery(db, recipient="receiver", delivery_id=lease["delivery_id"],
                                         token=lease["token"], success=True,
                                         result_artifact_id="artifact", now=NOW))
    with pytest.raises(LeaseConflict):
        claim_deliveries_idempotently(db, **kwargs)


def test_concurrent_same_key_claim_has_one_delivery_event(db):
    from synapse.persistence.delivery_store import claim_deliveries_idempotently

    send(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    def run(_):
        with closing(open_database(path)) as connection:
            barrier.wait(timeout=5)
            return claim_deliveries_idempotently(connection, principal="session", idempotency_key="poll",
                                                  recipient="receiver", now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].response == results[1].response
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1
