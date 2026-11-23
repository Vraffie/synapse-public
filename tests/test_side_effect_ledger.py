from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from synapse.persistence.migrations import open_database
from synapse.persistence.side_effect_ledger import (
    Entry, LedgerError, ProbeResult, StepKey, ensure_schema, get_state, reconcile_with_probe,
    record_completion, record_intent, recover_side_effects, resolve_unknown,
)

H1, H2 = "a" * 64, "b" * 64


def key(attempt=1, h=H1, step="deploy", task="t1"):
    return StepKey(task, attempt, h, step)


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "ledger.db"
    with closing(open_database(p)) as c:
        ensure_schema(c)
        ensure_schema(c)  # idempotent
    return p


@pytest.fixture
def db(path):
    with closing(open_database(path)) as c:
        yield c


def test_happy_path_and_idempotent_completion(db):
    assert record_intent(db, key()) is Entry.PROCEED
    record_completion(db, key())
    record_completion(db, key())
    assert get_state(db, key()) == "done"
    assert record_intent(db, key()) is Entry.ALREADY_DONE


def test_crash_between_intent_and_completion_becomes_unknown_never_replayed(path):
    with closing(open_database(path)) as c:
        assert record_intent(c, key()) is Entry.PROCEED  # process "crashes" here
    with closing(open_database(path)) as c:  # restart
        assert recover_side_effects(c, no_workers_survive=True) == (key(),)
        assert get_state(c, key()) == "unknown"
        assert record_intent(c, key()) is Entry.UNKNOWN  # replay refused
        with pytest.raises(LedgerError):
            record_completion(c, key())  # a stale worker cannot self-resolve
        assert recover_side_effects(c, no_workers_survive=True) == ()  # idempotent


def test_recovery_requires_attestation_and_leaves_done_alone(db):
    record_intent(db, key(step="a"))
    record_completion(db, key(step="a"))
    record_intent(db, key(step="b"))
    with pytest.raises(ValueError):
        recover_side_effects(db, no_workers_survive=False)
    assert recover_side_effects(db, no_workers_survive=True) == (key(step="b"),)
    assert get_state(db, key(step="a")) == "done"


def test_duplicate_replay_while_in_flight_is_refused(db):
    assert record_intent(db, key()) is Entry.PROCEED
    assert record_intent(db, key()) is Entry.IN_FLIGHT


def test_operator_resolves_unknown(db):
    record_intent(db, key())
    recover_side_effects(db, no_workers_survive=True)
    assert resolve_unknown(db, key(), outcome="done", actor="op", evidence="checked") == "done"
    assert resolve_unknown(db, key(), outcome="done", actor="op") == "done"  # idempotent
    with pytest.raises(LedgerError):
        resolve_unknown(db, key(), outcome="not_done", actor="op")  # conflicting
    assert record_intent(db, key()) is Entry.ALREADY_DONE


def test_not_done_does_not_unlock_same_key_but_new_attempt_is_new_key(db):
    record_intent(db, key())
    recover_side_effects(db, no_workers_survive=True)
    resolve_unknown(db, key(), outcome="not_done", actor="op")
    assert record_intent(db, key()) is Entry.NOT_DONE
    assert record_intent(db, key(attempt=2)) is Entry.PROCEED
    assert record_intent(db, key(h=H2)) is Entry.PROCEED  # different candidate hash is a different key


def test_resolve_refused_unless_unknown(db):
    with pytest.raises(LedgerError):
        resolve_unknown(db, key(), outcome="done", actor="op")  # no row
    record_intent(db, key())
    with pytest.raises(LedgerError):
        resolve_unknown(db, key(), outcome="done", actor="op")  # still intent
    with pytest.raises(ValueError):
        resolve_unknown(db, key(), outcome="maybe", actor="op")


def test_probe_resolution_and_inconclusive(db):
    record_intent(db, key())
    recover_side_effects(db, no_workers_survive=True)
    assert reconcile_with_probe(db, key(), lambda k: ProbeResult.INCONCLUSIVE) == "unknown"

    def boom(k):
        raise OSError("probe down")
    assert reconcile_with_probe(db, key(), boom) == "unknown"
    assert reconcile_with_probe(db, key(), lambda k: (ProbeResult.DONE, "rev abc live")) == "done"
    row = db.execute("SELECT resolution_source,resolved_by,evidence FROM side_effect_ledger").fetchone()
    assert row == ("probe", "probe", "rev abc live")
    with pytest.raises(LedgerError):
        reconcile_with_probe(db, key(), lambda k: ProbeResult.DONE)  # no longer unknown


def test_transition_log_is_append_only_and_complete(db):
    record_intent(db, key())
    recover_side_effects(db, no_workers_survive=True)
    resolve_unknown(db, key(), outcome="not_done", actor="op")
    states = [r[0] for r in db.execute("SELECT to_state FROM side_effect_ledger_transitions ORDER BY seq")]
    assert states == ["intent", "unknown", "not_done"]
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM side_effect_ledger_transitions")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE side_effect_ledger_transitions SET actor='x'")


def test_concurrent_claims_exactly_one_proceeds(path):
    n = 8
    barrier = Barrier(n)

    def claim(_):
        with closing(open_database(path)) as c:
            barrier.wait()
            return record_intent(c, key())

    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(claim, range(n)))
    assert results.count(Entry.PROCEED) == 1
    assert results.count(Entry.IN_FLIGHT) == n - 1


@pytest.mark.parametrize("bad", [
    dict(task_id="", attempt_no=1, candidate_sha256=H1, step="s"),
    dict(task_id="t", attempt_no=0, candidate_sha256=H1, step="s"),
    dict(task_id="t", attempt_no=True, candidate_sha256=H1, step="s"),
    dict(task_id="t", attempt_no=1, candidate_sha256="A" * 64, step="s"),
    dict(task_id="t", attempt_no=1, candidate_sha256="abc", step="s"),
    dict(task_id="t", attempt_no=1, candidate_sha256=H1, step=" "),
])
def test_key_validation(bad):
    with pytest.raises(ValueError):
        StepKey(**bad)


def test_failed_transaction_leaves_no_partial_row(db):
    db.execute("DROP TABLE side_effect_ledger_transitions")
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        record_intent(db, key())
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM side_effect_ledger").fetchone()[0] == 0
