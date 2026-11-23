"""M5.7 analytics read model: measured/estimated/unknown, coverage, bounds, and table path == event replay."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from synapse.application import analytics_views as av
from synapse.application.analytics_views import (AnalyticsTooLarge, Kind, read_run_analytics, replay_run_analytics,
                                                 reproducibility_diff)
from synapse.domain.contracts import EventEnvelope, MessageEnvelope, MessageId, MessageType, RunId, encode_payload
from synapse.persistence.attempt_store import reserve_attempt
from synapse.persistence.delivery_store import claim_deliveries, finish_delivery
from synapse.persistence.message_store import send_message
from synapse.persistence.mock_execution_store import set_attempt
from synapse.persistence.run_list import InvalidCursor
from synapse.persistence.run_snapshot import RunNotFound
from test_message_store import db  # noqa: F401  (migrated fixture with run 'run', agents, thread)

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def tx(db, action):
    db.execute("BEGIN IMMEDIATE")
    try:
        value = action()
        db.execute("COMMIT")
        return value
    except BaseException:
        db.execute("ROLLBACK")
        raise


def _event(db, aggregate_type, aggregate_id, kind, payload, run="run"):
    e = EventEnvelope.create(aggregate_type=aggregate_type, aggregate_id=aggregate_id, event_type=kind, payload=payload)
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (e.id, aggregate_type, aggregate_id, run, kind, 1, encode_payload(payload), e.occurred_at))
    return e


def reserve(db, task, attempt, version):
    tx(db, lambda: reserve_attempt(db, task_id=task, attempt_id=attempt, expected_task_version=version, principal="daemon"))
    db.execute("UPDATE tasks SET state='assigned' WHERE id=?", (task,))  # next retry may reserve again


def move(db, task, attempt, before, after):
    tx(db, lambda: set_attempt(db, attempt_id=attempt, before=before, after=after, run_id="run", task_id=task))


def observe(db, task, attempt, profile, **fields):
    values = {k: None for k in av.USAGE_FIELDS} | fields
    e = _event(db, "task_attempt", attempt, "agent.usage_observed", {"task_id": task, "source": "claude-cli", **values})
    db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,output_tokens,cache_read_tokens,"
               "cache_write_tokens,cost_usd,confidence,observed_at,source_event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               ("u" + e.id, attempt, profile, "claude-cli", *values.values(), "reported", e.occurred_at, e.id))


def estimate(db, attempt, method, tokens):
    _event(db, "task_attempt", attempt, "optimization.estimated", {"attempt_id": attempt, "method": method, "est_tokens": tokens})


def make_message(db, mid, key):
    return send_message(db, MessageEnvelope(id=MessageId(mid), run_id=RunId("run"), thread_id="thread",
                                            sender_instance_id="sender", type=MessageType.QUESTION, payload={"q": mid},
                                            created_at="2026-11-19T12:00:00+00:00"),
                        recipients=("receiver",), principal="sender", idempotency_key=key)


def claim(db, now=NOW, max_attempts=3):
    return tx(db, lambda: claim_deliveries(db, recipient="receiver", now=now, lease_seconds=10, max_attempts=max_attempts))


@pytest.fixture
def populated(db):
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('p1','mock','a','c','now')")
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('p2','mock','b','c','now')")
    for task, profile in (("t1", "p1"), ("t2", "p2"), ("t3", "p2")):
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,created_at,updated_at) "
                   "VALUES(?,'run',?,'assigned',0,?,'now','now')", (task, task, profile))
    reserve(db, "t1", "a1", 0)
    move(db, "t1", "a1", "created", "running"); move(db, "t1", "a1", "running", "failed")
    observe(db, "t1", "a1", "p1", input_tokens=10, output_tokens=5, cost_usd=0.1)
    reserve(db, "t1", "a2", 1)
    move(db, "t1", "a2", "created", "running"); move(db, "t1", "a2", "running", "completed")
    observe(db, "t1", "a2", "p1", input_tokens=20, cost_usd=0.2)
    observe(db, "t1", "a2", "p1", input_tokens=1, output_tokens=2, cost_usd=0.3)  # second observation, same attempt
    estimate(db, "a1", "chars/4", 100); estimate(db, "a2", "chars/4", 50)
    reserve(db, "t2", "a3", 0)  # no usage at all
    move(db, "t2", "a3", "created", "running"); move(db, "t2", "a3", "running", "completed")
    make_message(db, "m1", "k1"); make_message(db, "m2", "k2")
    first = claim(db)
    by_msg = {lease.message_id: lease for lease in first}
    tx(db, lambda: finish_delivery(db, recipient="receiver", delivery_id=by_msg["m2"].delivery_id, token=by_msg["m2"].token,
                                   success=False, max_attempts=1, now=NOW))
    again, = claim(db, NOW + timedelta(seconds=11))  # m1's lease expired: redelivery
    db.execute("INSERT INTO artifacts VALUES('res','h','text/plain',0,'p','{}','now')")
    tx(db, lambda: finish_delivery(db, recipient="receiver", delivery_id=again.delivery_id, token=again.token,
                                   success=True, result_artifact_id="res", now=NOW + timedelta(seconds=12)))
    make_message(db, "m3", "k3")  # never claimed
    return db


def test_tables_view_values_and_kinds(populated):
    v = read_run_analytics(populated, "run")
    assert (v.attempts.value, v.retries.value) == (3, 1)
    assert {k: q.value for k, q in v.outcome_mix.items()} == {"completed": 2, "failed": 1}
    u = v.usage
    assert (u["input_tokens"].value, u["output_tokens"].value, u["cache_read_tokens"].value) == (31, 7, None)
    assert u["cost_usd"].value == pytest.approx(0.6)
    assert all(q.kind is Kind.MEASURED and q.basis == "provider_reported" for k, q in u.items() if q.value is not None)
    assert v.attempts_with_any_usage == 2
    m = v.messages
    assert (m.sent.value, m.deliveries_created.value, m.claimed.value, m.redeliveries.value,
            m.acknowledged.value, m.dead_lettered.value) == (3, 3, 2, 1, 1, 1)
    assert [t.task_id for t in v.tasks] == ["t1", "t2"] and v.tasks[0].retries.value == 1
    assert set(v.per_profile) == {"p1", "p2"} and v.per_profile["p1"].usage["input_tokens"].value == 31


def test_unknown_stays_unknown_and_coverage_flags_lower_bound(populated):
    u = read_run_analytics(populated, "run").usage
    cache = u["cache_read_tokens"]
    assert (cache.kind, cache.value, cache.known, cache.of, cache.lower_bound) == (Kind.UNKNOWN, None, 0, 3, False)
    assert (u["input_tokens"].known, u["input_tokens"].of, u["input_tokens"].lower_bound) == (2, 3, True)
    assert (u["output_tokens"].known, u["output_tokens"].lower_bound) == (2, True)  # a2's first observation had none
    task2 = read_run_analytics(populated, "run").tasks[1]  # attempt with no usage: unknown, not 0
    assert all(q.value is None and q.kind is Kind.UNKNOWN for q in task2.usage.values())
    assert read_run_analytics(populated, "run").tasks[0].usage["input_tokens"].lower_bound is False  # both attempts known


def test_estimates_are_separate_and_never_merged_into_measured(populated):
    v = read_run_analytics(populated, "run")
    e = v.estimated["chars/4"]["est_tokens"]
    assert (e.kind, e.value, e.basis, e.known, e.of, e.lower_bound) == (Kind.ESTIMATED, 150, "chars/4", 2, 3, True)
    assert v.usage["input_tokens"].value == 31 and "est_tokens" not in v.usage
    assert all(q.kind is not Kind.ESTIMATED for q in v.usage.values())
    # An estimate for a run whose measured usage is wholly unknown must not turn it into a number.
    _event(populated, "task_attempt", "a3", "optimization.estimated", {"attempt_id": "a3", "method": "m2", "est_tokens": 7})
    v = read_run_analytics(populated, "run")
    assert v.tasks[1].usage["input_tokens"].value is None and v.estimated["m2"]["est_tokens"].value == 7


def test_malformed_and_unattributable_estimates_are_counted_not_summed(populated):
    for payload in ({"attempt_id": "ghost", "method": "x", "est_tokens": 5}, {"attempt_id": "a1", "method": "x", "est_tokens": -1},
                    {"attempt_id": "a1", "method": "x", "est_tokens": True}, {"attempt_id": "a1", "est_tokens": 3}):
        _event(populated, "task_attempt", "a1", "optimization.estimated", payload)
    for path in (read_run_analytics, replay_run_analytics):
        v = path(populated, "run")
        assert v.ignored_estimate_events == 4 and "x" not in v.estimated


def test_replay_equals_tables_on_populated_database(populated):
    tables, replay = read_run_analytics(populated, "run"), replay_run_analytics(populated, "run")
    assert reproducibility_diff(tables, replay) == []
    assert tables.to_dict()["usage"] == replay.to_dict()["usage"] and tables.tasks == replay.tasks
    assert tables.estimated == replay.estimated and replay.replay_ignored_events == 0
    # The gaps are declared, typed and visible rather than hidden.
    assert replay.source == "events" and replay.per_profile is None
    assert replay.messages.deliveries_created.kind is Kind.UNKNOWN and replay.messages.deliveries_created.value is None
    assert replay.not_reproducible == av.NOT_REPRODUCIBLE_FROM_EVENTS and tables.not_reproducible == ()


def test_replay_detects_a_divergent_table(populated):
    populated.execute("DROP TRIGGER usage_observations_no_update")
    populated.execute("UPDATE usage_observations SET input_tokens=input_tokens+1 WHERE attempt_id='a1'")
    assert "usage" in reproducibility_diff(read_run_analytics(populated, "run"), replay_run_analytics(populated, "run"))


def test_replay_ignores_orphan_events_visibly(populated):
    _event(populated, "task_attempt", "ghost", "task_attempt.state_changed", {"after": "failed"})
    _event(populated, "task_attempt", "ghost", "agent.usage_observed", {"input_tokens": 999})
    v = replay_run_analytics(populated, "run")
    assert v.replay_ignored_events == 2 and v.usage["input_tokens"].value == 31


@pytest.mark.parametrize("path", [read_run_analytics, replay_run_analytics])
def test_empty_run_is_unknown_not_zero(db, path):
    v = path(db, "other")
    assert v.attempts.value == 0 and v.retries.value == 0 and v.tasks == () and v.next_cursor is None
    assert all(q.kind is Kind.UNKNOWN and q.value is None and q.of == 0 and not q.lower_bound for q in v.usage.values())
    assert v.messages.sent.value == 0 and v.estimated == {}


@pytest.mark.parametrize("path", [read_run_analytics, replay_run_analytics])
def test_unknown_run_and_bad_ids(db, path):
    with pytest.raises(RunNotFound):
        path(db, "nope")
    with pytest.raises(ValueError):
        path(db, "bad id!")
    assert not db.in_transaction


@pytest.mark.parametrize("path", [read_run_analytics, replay_run_analytics])
def test_requires_own_transaction_and_never_writes(populated, path):
    before = populated.total_changes
    path(populated, "run")
    assert populated.total_changes == before and not populated.in_transaction
    populated.execute("BEGIN")
    with pytest.raises(RuntimeError):
        path(populated, "run")
    populated.execute("ROLLBACK")


@pytest.mark.parametrize("path", [read_run_analytics, replay_run_analytics])
def test_task_pagination_is_keyset_bounded_and_totals_cover_whole_run(populated, path):
    first = path(populated, "run", limit=1)
    assert [t.task_id for t in first.tasks] == ["t1"] and first.next_cursor
    second = path(populated, "run", limit=1, cursor=first.next_cursor)
    assert [t.task_id for t in second.tasks] == ["t2"] and second.next_cursor is None
    assert first.attempts.value == second.attempts.value == 3  # totals are never page-scoped
    for bad in (0, av.MAX_PAGE + 1, True, "1"):
        with pytest.raises(ValueError):
            path(populated, "run", limit=bad)
    for cursor in ("!!", "e30"):
        with pytest.raises(InvalidCursor):
            path(populated, "run", cursor=cursor)
    foreign = av.encode_cursor("run", "t1")  # a run-list cursor is not a task cursor
    with pytest.raises(InvalidCursor):
        path(populated, "run", cursor=foreign)


@pytest.mark.parametrize("path,cap", [(read_run_analytics, "MAX_ATTEMPTS"), (replay_run_analytics, "MAX_EVENTS")])
def test_hard_caps_refuse_instead_of_truncating(populated, path, cap, monkeypatch):
    monkeypatch.setattr(av, cap, 2)
    with pytest.raises(AnalyticsTooLarge):
        path(populated, "run")
    assert not populated.in_transaction


def _raw_usage_event(db, attempt, raw):
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (f"raw{db.total_changes}", "task_attempt", attempt, "run", "agent.usage_observed", 1, raw, "now"))


@pytest.mark.parametrize("raw", ['{"input_tokens": 1' + "0" * 400 + "}", "[" * 100_000, "[1,2]", "7", "null", "not json",
                                 '{"input_tokens": NaN, "cost_usd": Infinity}'])
def test_hostile_usage_payload_never_raises_and_is_counted(populated, raw):
    clean = replay_run_analytics(populated, "run")
    _raw_usage_event(populated, "a3", raw)
    for path in (read_run_analytics, replay_run_analytics):
        v = path(populated, "run")  # must not raise (OverflowError / RecursionError)
        assert v.usage["input_tokens"].value == 31
    v = replay_run_analytics(populated, "run")
    if raw.startswith('{"input_tokens"'):  # a parseable object: its values are unknown, the attempt contributes nothing
        assert v.replay_ignored_events == 0 and v.attempts_with_any_usage == 2
    else:
        assert v.replay_ignored_events == clean.replay_ignored_events + 1


def test_hostile_estimate_payloads_never_raise(populated):
    for raw in ("[" * 100_000, '{"attempt_id": [], "method": "x", "est_tokens": 1}', '{"attempt_id":"a1","method":"x","est_tokens":1' + "0" * 400 + "}"):
        populated.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                          "VALUES(?,?,?,?,?,?,?,?)", (f"e{populated.total_changes}", "task_attempt", "a1", "run", "optimization.estimated", 1, raw, "now"))
    for path in (read_run_analytics, replay_run_analytics):
        v = path(populated, "run")
        assert v.ignored_estimate_events == 3 and "x" not in v.estimated


def test_hostile_table_values_are_unknown_on_both_paths_and_paths_agree(populated):
    # The schema has no CHECK on token columns, so a probe (or a buggy writer) can store TEXT or negatives.
    observe(populated, "t2", "a3", "p2", input_tokens="lots", output_tokens=-5, cache_read_tokens=3, cost_usd="free")
    tables, replay = read_run_analytics(populated, "run"), replay_run_analytics(populated, "run")
    assert reproducibility_diff(tables, replay) == []
    assert tables.usage == replay.usage and tables.tasks == replay.tasks
    assert tables.usage["input_tokens"].value == 31 and tables.usage["output_tokens"].value == 7
    assert tables.usage["cache_read_tokens"].value == 3 and tables.attempts_with_any_usage == 3
    # Corrupt an existing row directly, the way a probe would, after dropping the append-only trigger.
    populated.execute("DROP TRIGGER usage_observations_no_update")
    populated.execute("UPDATE usage_observations SET input_tokens='x', cost_usd='y' WHERE attempt_id='a1'")
    v = read_run_analytics(populated, "run")
    assert v.usage["input_tokens"].value == 21 and v.usage["cost_usd"].value == pytest.approx(0.5)  # a1's cost is now unknown; a2's two stay


def test_observation_with_every_field_null_is_not_counted_as_usage(populated):
    observe(populated, "t2", "a3", "p2")  # all NULL
    for path in (read_run_analytics, replay_run_analytics):
        assert path(populated, "run").attempts_with_any_usage == 2


def test_replay_counts_are_lower_bounds_when_events_were_ignored(populated):
    healthy = replay_run_analytics(populated, "run")
    assert healthy.attempts.lower_bound is False and healthy.attempts.known == healthy.attempts.of
    _raw_usage_event(populated, "a1", "[" * 1000)
    v = replay_run_analytics(populated, "run")
    assert v.replay_ignored_events == 1
    for q in (v.attempts, v.retries, v.messages.sent, v.messages.claimed, v.tasks[0].attempts, *v.outcome_mix.values()):
        assert q.kind is Kind.MEASURED and q.lower_bound is True and q.known < q.of
    assert v.attempts.value == 3  # the value itself is unchanged; only its coverage is reduced
    assert read_run_analytics(populated, "run").attempts.lower_bound is False


def test_task_page_computes_only_the_page(populated, monkeypatch):
    seen = []
    real = av._usage
    monkeypatch.setattr(av, "_usage", lambda observations, of: seen.append(of) or real(observations, of))
    v = read_run_analytics(populated, "run", limit=1)
    assert [t.task_id for t in v.tasks] == ["t1"] and v.next_cursor
