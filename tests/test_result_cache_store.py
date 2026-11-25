"""SQL result cache store (M5.4 cache 3/3): migration 0020, triggers, CHECKs, races, tamper, round trip."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from synapse.application import result_cache as rc
from synapse.application.cache_key import CacheKeyInputs, derive_cache_key
from synapse.application.context_manifest import ManifestEntry, build_manifest
from synapse.application.result_cache import (
    ArtifactFacts, CacheEntry, CacheOutcome, CacheResult, CacheStore, MissReason, RecordStatus, lookup, record,
)
from synapse.persistence.backup import schema_fingerprint
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.result_cache_store import CorruptCacheEntry, SqliteCacheStore

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
H = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
ARTIFACT = H("result bytes")
APPEND_ONLY = "result cache entries are append-only"


def request(**over) -> CacheKeyInputs:
    manifest = build_manifest("t1", repo_head="a" * 40, worktree_clean=True,
                              entries=[ManifestEntry("spec", "t1", H("spec")), ManifestEntry("artifact", "p1", H("p"), 6)])
    base = CacheKeyInputs(
        task_class="read_only_analysis", operator_marked_cacheable=True, approval_gated=False,
        policy_version="policy-1", approval_policy_sha256=H("approval-policy-1"),
        provider="claude", profile_id="default", cli_version="2.1.0", model="model-a",
        allowed_tools=["Read", "Grep"], permission_mode="plan", settings={"max_turns": 3},
        spec_sha256=H("spec"), manifest=manifest, validator_sha256s=[H("pytest")])
    return replace(base, **over)


def result(**over) -> CacheResult:
    base = CacheResult(origin_attempt_id="att-1", outcome="succeeded", budget_blocked=False,
                       result_sha256=ARTIFACT, result_size=12, created_marker="seq:1")
    return replace(base, **over)


def key_of(**over) -> str:
    return derive_cache_key(request(**over)).sha256


def rec(store, req, res):
    return record(store, req, res, expected_key_sha256=derive_cache_key(req).sha256)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "synapse.db"
    with closing(open_database(path)) as db:
        apply_migrations(db, MIGRATIONS)
    return path


@pytest.fixture
def db(db_path):
    with closing(open_database(db_path)) as connection:
        yield connection


def raw_row(db, key):
    return db.execute("SELECT key_sha256, entry_json, result_artifact_id, origin_attempt_id, created_at,"
                      " invalidated_at FROM result_cache WHERE key_sha256 = ?", (key,)).fetchone()


def insert(db, key, entry_json, artifact=ARTIFACT, attempt="att-1", created="seq:1", verb="INSERT"):
    return db.execute(f"{verb} INTO result_cache(key_sha256, entry_json, result_artifact_id, origin_attempt_id,"
                      " created_at) VALUES (?, ?, ?, ?, ?)", (key, entry_json, artifact, attempt, created)).rowcount


def entry_for(**result_over) -> CacheEntry:
    """The exact entry record() would store for request() and result(**result_over)."""
    from synapse.application.result_cache import InMemoryCacheStore
    mem = InMemoryCacheStore()
    assert rec(mem, request(), result(**result_over)).status is RecordStatus.STORED
    return mem.get(key_of())


def assert_never_authority(decision) -> None:
    assert decision.counts_as_verification is False
    assert decision.counts_as_review is False
    assert decision.satisfies_approval is False
    assert decision.counts_as_mandatory_check is False
    assert decision.requires_verification is True
    if decision.hit is not None:
        assert decision.hit.counts_as_verification is False and decision.hit.satisfies_approval is False


# --- migration -----------------------------------------------------------------------------------

def _seed_v17(db) -> None:
    """Rows a schema-17 daemon could have written: workflow, profile, run, task, attempts, mark, event."""
    db.execute("BEGIN")
    db.execute("INSERT INTO workflow_versions VALUES ('wf', '1', '{\"nodes\":[]}', ?, '2026-01-01T00:00:00Z')",
               (H("wf"),))
    db.execute("INSERT INTO profiles(id, provider, display_name, config_ref, created_at)"
               " VALUES ('default', 'mock', 'Default \u00e6\u00f8\u00e5', 'cfg', '2026-01-01T00:00:00Z')")
    db.execute("INSERT INTO runs VALUES ('run-1', 'wf', '1', 'running', 3, '2026-01-01T00:00:00Z',"
               " '2026-01-01T00:00:01Z')")
    db.execute("INSERT INTO tasks VALUES ('task-1', 'run-1', 'n1', 'running', 2, 'default', 2,"
               " '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z')")
    db.execute("INSERT INTO task_attempts VALUES ('att-1', 'task-1', 1, 'default', 'failed', 'sess-1', '/w/1',"
               " '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z', 1)")
    db.execute("INSERT INTO task_attempts(id, task_id, attempt_no, profile_id, state)"
               " VALUES ('att-2', 'task-1', 2, 'default', 'running')")
    db.execute("INSERT INTO attempt_spawn_marks VALUES ('att-1', '2026-01-01T00:00:00Z')")
    db.execute("INSERT INTO events(id, aggregate_type, aggregate_id, run_id, type, schema_version, payload_json,"
               " occurred_at) VALUES ('ev-1', 'run', 'run-1', 'run-1', 'run.started', 1, '{\"a\":1}',"
               " '2026-01-01T00:00:00Z')")
    db.execute("COMMIT")


def _all_rows(db) -> dict[str, list[tuple]]:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                       " AND name NOT IN ('schema_migrations') ORDER BY name")]
    return {t: sorted(db.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr)  # some are WITHOUT ROWID
            for t in tables if t != "result_cache"}


def test_upgrade_from_0019_keeps_existing_rows_and_adds_only_the_cache_table(tmp_path):
    with closing(open_database(tmp_path / "v17.db")) as old:
        assert apply_migrations(old, MIGRATIONS, limit=19)[-1] == 19
        _seed_v17(old)
        rows_before = _all_rows(old)
        assert rows_before["attempt_spawn_marks"] == [("att-1", "2026-01-01T00:00:00Z")]
        assert len(rows_before["task_attempts"]) == 2 and rows_before["events"]
        before = {r[0] for r in old.execute("SELECT name FROM sqlite_master")}
        assert "result_cache" not in before
        assert apply_migrations(old, MIGRATIONS) == [20]
        assert apply_migrations(old, MIGRATIONS) == []
        after = {r[0] for r in old.execute("SELECT name FROM sqlite_master")}
        assert after - before == {"result_cache", "sqlite_autoindex_result_cache_1", "result_cache_first_writer_wins",
                                  "result_cache_no_update", "result_cache_no_delete"}
        assert max(r[0] for r in old.execute("SELECT version FROM schema_migrations")) == 20
        assert _all_rows(old) == rows_before  # every pre-existing row is unchanged, byte for byte
        assert old.execute("SELECT count(*) FROM result_cache").fetchone() == (0,)
    with closing(open_database(tmp_path / "v17.db")) as upgraded:
        fresh = schema_fingerprint(MIGRATIONS, 20)
        actual = {(k, n): (t, " ".join(s.split()) if s else None) for k, n, t, s in upgraded.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}
        assert actual == fresh


# --- triggers ------------------------------------------------------------------------------------

def test_first_writer_wins_against_every_insert_form_and_append_only_holds(db):
    entry = entry_for()
    key = entry.key_sha256
    assert insert(db, key, entry.entry_json()) == 1
    first = raw_row(db, key)
    evil = entry_for(result_sha256=H("evil"), origin_attempt_id="att-evil")
    for verb in ("INSERT", "INSERT OR REPLACE", "INSERT OR IGNORE", "REPLACE"):
        assert insert(db, key, evil.entry_json(), H("evil"), "att-evil", verb=verb) == 0
        assert raw_row(db, key) == first
    db.execute("INSERT INTO result_cache(key_sha256, entry_json, result_artifact_id, origin_attempt_id, created_at)"
               " VALUES (?, ?, ?, 'att-evil', 'seq:1') ON CONFLICT(key_sha256) DO UPDATE SET"
               " invalidated_at = 'x'", (key, evil.entry_json(), H("evil")))
    assert raw_row(db, key) == first
    assert db.execute("SELECT count(*) FROM result_cache").fetchone() == (1,)

    assert db.execute("UPDATE result_cache SET invalidated_at = 'inv:1' WHERE key_sha256 = ?", (key,)).rowcount == 1
    for sql in ("UPDATE result_cache SET invalidated_at = 'inv:2'",
                "UPDATE result_cache SET invalidated_at = NULL",
                f"UPDATE result_cache SET result_artifact_id = '{H('evil')}'",
                "UPDATE result_cache SET created_at = 'seq:0'",
                "DELETE FROM result_cache"):
        with pytest.raises(sqlite3.IntegrityError, match=APPEND_ONLY):
            db.execute(sql)
    assert insert(db, key, evil.entry_json(), H("evil"), "att-evil", verb="INSERT OR REPLACE") == 0
    assert raw_row(db, key)[1:] == first[1:5] + ("inv:1",)


def test_update_that_changes_anything_besides_invalidation_is_refused_even_alongside_it(db):
    entry = entry_for()
    insert(db, entry.key_sha256, entry.entry_json())
    with pytest.raises(sqlite3.IntegrityError, match=APPEND_ONLY):
        db.execute("UPDATE result_cache SET invalidated_at = 'inv', origin_attempt_id = 'att-2'")
    with pytest.raises(sqlite3.IntegrityError, match=APPEND_ONLY):
        db.execute("UPDATE result_cache SET invalidated_at = 'inv', entry_json = CAST(entry_json AS BLOB)")
    assert raw_row(db, entry.key_sha256)[5] is None


# --- CHECKs --------------------------------------------------------------------------------------

@pytest.mark.parametrize("key, error", [
    (None, "NOT NULL constraint failed"),
    ("Z" * 64, "CHECK constraint failed"),
    ("A" * 64, "CHECK constraint failed"),
    ("a" * 63, "CHECK constraint failed"),
    ("a" * 65, "CHECK constraint failed"),
    (b"a" * 64, "CHECK constraint failed"),
])
def test_key_must_be_lowercase_hex_text(db, key, error):
    doc = json.dumps({"key_sha256": key if isinstance(key, str) else None, "result_sha256": ARTIFACT,
                      "origin_attempt_id": "att-1", "created_marker": "seq:1"})
    with pytest.raises(sqlite3.IntegrityError, match=error):
        insert(db, key, doc)
    assert db.execute("SELECT count(*) FROM result_cache").fetchone() == (0,)


def test_entry_json_must_be_a_bounded_json_object_agreeing_with_the_columns(db):
    entry = entry_for()
    key, good = entry.key_sha256, entry.entry_json()
    doc = json.loads(good)
    bad = [
        "not json",
        '{"key_sha256": NaN}',
        json.dumps([doc]),
        good.encode(),  # a BLOB with the same bytes
        json.dumps({**doc, "pad": "x" * rc.MAX_ENTRY_BYTES}),
    ]
    for entry_json in bad:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            insert(db, key, entry_json)
    for column, value in (("artifact", H("other")), ("attempt", "att-2"), ("created", "seq:2")):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            insert(db, key, good, **{column: value})
    padded = json.dumps({**doc, "pad": ""})
    pad = rc.MAX_ENTRY_BYTES - len(padded.encode())
    at_bound = json.dumps({**doc, "pad": "x" * pad})
    assert len(at_bound.encode()) == rc.MAX_ENTRY_BYTES
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        insert(db, key, json.dumps({**doc, "pad": "x" * (pad + 1)}))
    assert insert(db, key, at_bound) == 1  # the CHECK bound is exactly MAX_ENTRY_BYTES


def test_invalidation_marker_must_be_short_text(db):
    entry = entry_for()
    insert(db, entry.key_sha256, entry.entry_json())
    for value in ("", "x" * 257, b"inv"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute("UPDATE result_cache SET invalidated_at = ?", (value,))


# --- store and port ------------------------------------------------------------------------------

def test_store_satisfies_port_and_round_trips_through_lookup_and_record(db):
    store = SqliteCacheStore(db)
    assert isinstance(store, CacheStore)
    assert store.get(key_of()) is None
    miss = lookup(store, request())
    assert miss.outcome is CacheOutcome.MISS and miss.reason == MissReason.NOT_FOUND
    assert rec(store, request(), result()).status is RecordStatus.STORED
    assert store.get(key_of()) == entry_for()
    hit = lookup(store, request())
    assert hit.is_hit and (hit.result_sha256, hit.result_size) == (ARTIFACT, 12)
    assert hit.hit.origin_attempt_id == "att-1" and hit.key_sha256 == key_of()
    again = rec(store, request(), result(result_sha256=H("second"), origin_attempt_id="att-2"))
    assert again.status is RecordStatus.ALREADY_PRESENT and again.reason == "first_writer_wins"
    assert lookup(store, request()).hit.origin_attempt_id == "att-1"
    assert lookup(store, request(model="model-b")).reason == MissReason.NOT_FOUND
    row = raw_row(db, key_of())
    assert row[2:5] == (ARTIFACT, "att-1", "seq:1") and row[5] is None


def test_long_but_bounded_entry_fits_the_sql_bounds(db):
    wide = "\U0001F600" * rc.MAX_TEXT
    store = SqliteCacheStore(db)
    out = rec(store, request(), result(origin_attempt_id=wide, created_marker=wide, result_size=rc.MAX_RESULT_BYTES))
    assert out.status is RecordStatus.STORED
    assert lookup(store, request()).hit.origin_attempt_id == wide


def test_invalidation_is_one_way_and_record_cannot_resurrect(db):
    store = SqliteCacheStore(db)
    rec(store, request(), result())
    assert store.invalidate(key_of(), "inv:1") is True
    assert store.invalidate(key_of(), "inv:2") is False
    assert store.invalidate(H("absent"), "inv:1") is False
    assert lookup(store, request()).reason == MissReason.INVALIDATED
    assert rec(store, request(), result(origin_attempt_id="att-2")).status is RecordStatus.ALREADY_PRESENT
    decision = lookup(store, request())
    assert decision.reason == MissReason.INVALIDATED and store.get(key_of()).invalidated_marker == "inv:1"


def test_put_refuses_inconsistent_arguments_and_rolls_back(db):
    store = SqliteCacheStore(db)
    entry = entry_for()
    with pytest.raises(ValueError):
        store.put(H("other"), entry)
    with pytest.raises(ValueError):
        store.put(entry.key_sha256, replace(entry, invalidated_marker="inv"))
    with pytest.raises(sqlite3.IntegrityError):  # oversized: the CHECK refuses, nothing is left behind
        store.put(entry.key_sha256, replace(entry, key_material_json=json.dumps({"x": "y" * rc.MAX_ENTRY_BYTES})))
    assert not db.in_transaction and db.execute("SELECT count(*) FROM result_cache").fetchone() == (0,)
    db.execute("BEGIN IMMEDIATE")  # inside a caller's transaction put joins it
    assert store.put(entry.key_sha256, entry) is True
    db.execute("ROLLBACK")
    assert store.get(entry.key_sha256) is None


def _seed_caller_work(db):
    db.execute("INSERT INTO workflow_versions VALUES ('wf', '1', '{}', ?, 't')", (H("wf"),))


def test_failed_put_inside_a_caller_transaction_leaves_it_open_and_intact(db):
    store = SqliteCacheStore(db)
    entry = entry_for()
    store.put(entry.key_sha256, entry)
    store.invalidate(entry.key_sha256, "inv:1")
    db.execute("BEGIN IMMEDIATE")
    _seed_caller_work(db)
    oversized = replace(entry_for(), key_sha256=key_of(model="model-b"),
                        key_material_json=json.dumps({"x": "y" * rc.MAX_ENTRY_BYTES}))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        store.put(oversized.key_sha256, oversized)
    assert db.in_transaction
    assert store.put(entry.key_sha256, entry) is False  # the transaction is still usable
    db.execute("COMMIT")
    assert db.execute("SELECT count(*) FROM workflow_versions WHERE workflow_id = 'wf'").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM result_cache").fetchone() == (1,)
    assert store.get(entry.key_sha256).invalidated_marker == "inv:1"


def test_invalidate_that_fails_inside_a_caller_transaction_keeps_the_callers_work(db):
    store = SqliteCacheStore(db)
    entry = entry_for()
    store.put(entry.key_sha256, entry)
    db.execute("BEGIN IMMEDIATE")
    _seed_caller_work(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        store.invalidate(entry.key_sha256, "x" * 257)
    assert db.in_transaction
    assert store.invalidate(entry.key_sha256, "inv:ok") is True  # the transaction is still usable
    db.execute("COMMIT")
    assert db.execute("SELECT count(*) FROM workflow_versions WHERE workflow_id = 'wf'").fetchone() == (1,)
    assert store.get(entry.key_sha256).invalidated_marker == "inv:ok"


def test_put_inside_a_caller_transaction_that_rolls_back_leaves_no_row(db):
    store = SqliteCacheStore(db)
    entry = entry_for()
    db.execute("BEGIN IMMEDIATE")
    _seed_caller_work(db)
    assert store.put(entry.key_sha256, entry) is True
    assert db.in_transaction
    db.execute("ROLLBACK")
    assert store.get(entry.key_sha256) is None
    assert db.execute("SELECT count(*) FROM workflow_versions WHERE workflow_id = 'wf'").fetchone() == (0,)
    assert store.put(entry.key_sha256, entry) is True  # nothing left behind to block a later first writer


def test_store_error_on_put_is_a_store_error_record_outcome(db):
    store = SqliteCacheStore(db)
    db.execute("DROP TABLE result_cache")  # scratch database: simulate a broken store
    out = rec(store, request(), result())
    assert out.status is RecordStatus.STORE_ERROR
    assert lookup(store, request()).reason == MissReason.STORE_ERROR


# --- concurrency ---------------------------------------------------------------------------------

def test_two_connections_racing_first_writer_exactly_one_wins(db_path):
    for round_ in range(15):
        req = request(spec_sha256=H(f"spec-{round_}"))
        barrier = threading.Barrier(2)
        outcomes: dict[str, RecordStatus] = {}

        def worker(name):
            with closing(open_database(db_path)) as connection:
                store = SqliteCacheStore(connection)
                barrier.wait()
                outcomes[name] = rec(store, req, result(origin_attempt_id=name, result_sha256=H(name))).status

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("att-a", "att-b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(outcomes.values()) == sorted([RecordStatus.STORED, RecordStatus.ALREADY_PRESENT])
        winner = next(n for n, s in outcomes.items() if s is RecordStatus.STORED)
        with closing(open_database(db_path)) as check:
            hit = lookup(SqliteCacheStore(check), req)
            assert hit.is_hit and hit.hit.origin_attempt_id == winner and hit.result_sha256 == H(winner)


def test_writer_behind_an_uncommitted_first_writer_cannot_decide_until_it_commits(db_path):
    # Deterministic, no timing: with busy_timeout 0 the second writer fails fast instead of waiting, which
    # shows it needs the write lock before it decides anything (no decision on a stale snapshot).
    with closing(open_database(db_path)) as first, closing(open_database(db_path)) as second:
        second.execute("PRAGMA busy_timeout = 0")
        late = SqliteCacheStore(second)
        assert late.get(key_of()) is None  # its snapshot before the first writer
        first.execute("BEGIN IMMEDIATE")
        assert rec(SqliteCacheStore(first), request(), result(origin_attempt_id="att-a")).status is RecordStatus.STORED
        entry_b = replace(entry_for(), origin_attempt_id="att-b")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            late.put(key_of(), entry_b)
        assert not second.in_transaction
        assert rec(late, request(), result(origin_attempt_id="att-b")).status is RecordStatus.STORE_ERROR
        first.execute("COMMIT")
        assert rec(late, request(), result(origin_attempt_id="att-b")).status is RecordStatus.ALREADY_PRESENT
        assert lookup(late, request()).hit.origin_attempt_id == "att-a"


# --- tamper --------------------------------------------------------------------------------------

def _tamper(db, entry_json, key):
    """Hand-edit a row the way only someone bypassing the daemon could: drop the guard, edit, restore."""
    db.execute("DROP TRIGGER result_cache_no_update")
    db.execute("UPDATE result_cache SET entry_json = ? WHERE key_sha256 = ?", (entry_json, key))


def test_tampered_field_is_a_digest_miss_never_a_hit(db):
    store = SqliteCacheStore(db)
    rec(store, request(), result())
    doc = json.loads(raw_row(db, key_of())[1])
    _tamper(db, json.dumps({**doc, "result_size": 13}), key_of())
    decision = lookup(store, request())
    assert decision.outcome is CacheOutcome.MISS and decision.reason == MissReason.ENTRY_DIGEST_MISMATCH


def test_tampered_material_is_a_material_miss(db):
    store = SqliteCacheStore(db)
    rec(store, request(), result())
    doc = json.loads(raw_row(db, key_of())[1])
    _tamper(db, json.dumps({**doc, "key_material": {**doc["key_material"], "model": "model-b"}}), key_of())
    assert lookup(store, request()).reason == MissReason.MATERIAL_MISMATCH


def test_duplicate_json_keys_pass_the_check_but_are_rejected_on_read(db):
    store = SqliteCacheStore(db)
    good = entry_for().entry_json()
    key = key_of()
    # SQLite's json_extract reads the first duplicate (so the column CHECK passes), Python's json.loads
    # would read the last: without the read-side guard the entry would point at another artifact.
    evil = H("evil")
    dup = good[:-1] + f',"result_sha256":"{evil}"' + "}"
    assert insert(db, key, dup) == 1
    assert db.execute("SELECT json_extract(entry_json, '$.result_sha256') FROM result_cache").fetchone() == (ARTIFACT,)
    assert json.loads(dup)["result_sha256"] == evil
    with pytest.raises(CorruptCacheEntry, match="duplicate JSON key"):
        store.get(key)
    decision = lookup(store, request())
    assert decision.outcome is CacheOutcome.MISS and decision.reason == MissReason.STORE_ERROR


@pytest.mark.parametrize("change", [
    lambda d: {**d, "extra": 1},
    lambda d: {k: v for k, v in d.items() if k != "entry_digest"},
])
def test_wrong_field_set_is_rejected_on_read(db, change):
    store = SqliteCacheStore(db)
    doc = json.loads(entry_for().entry_json())
    assert insert(db, key_of(), json.dumps(change(doc))) == 1
    with pytest.raises(CorruptCacheEntry):
        store.get(key_of())
    assert lookup(store, request()).reason == MissReason.STORE_ERROR


def test_overflowing_number_in_key_material_is_corrupt_not_a_crash(db):
    store = SqliteCacheStore(db)
    good = entry_for().entry_json()
    assert '"worktree_clean":true' in good
    tampered = good.replace('"worktree_clean":true', '"worktree_clean":1e400')
    assert insert(db, key_of(), tampered) == 1  # json_valid accepts it
    with pytest.raises(CorruptCacheEntry, match="canonical"):
        store.get(key_of())
    assert lookup(store, request()).reason == MissReason.STORE_ERROR


def test_wrong_field_types_survive_decode_but_lookup_misses(db):
    store = SqliteCacheStore(db)
    doc = json.loads(entry_for().entry_json())
    assert insert(db, key_of(), json.dumps({**doc, "result_size": True})) == 1
    assert lookup(store, request()).reason == MissReason.MALFORMED_ENTRY


# --- invariant 10 --------------------------------------------------------------------------------

def test_a_store_hit_is_never_a_verification_and_writes_nothing(db):
    store = SqliteCacheStore(db)
    rec(store, request(), result())
    before = db.total_changes
    plain = lookup(store, request())
    assert plain.is_hit and plain.artifact_hash_checked is False
    assert_never_authority(plain)
    checked = lookup(store, request(), verifier=lambda sha, size: ArtifactFacts(sha, size))
    assert checked.is_hit and checked.artifact_hash_checked is True
    assert_never_authority(checked)  # even a verified artifact hash is not a verification
    assert db.total_changes == before  # a lookup records no verification, review, approval or event
    assert "counts_as_verification" not in {d[0] for d in db.execute("SELECT * FROM result_cache").description}
