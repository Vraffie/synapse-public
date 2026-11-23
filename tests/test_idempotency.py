import sqlite3
import pytest

from synapse.persistence.idempotency import (
    IdempotencyConflict, canonical_request_hash, execute_idempotent,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE idempotency_keys (principal TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL, request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(principal,operation,key))")
    conn.execute("CREATE TABLE effects (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    yield conn
    conn.close()


def call(db, request, key="request-1", principal="alice", execute=None):
    if execute is None:
        def execute(conn):
            conn.execute("INSERT INTO effects(value) VALUES (?)", (request["value"],))
            return {"accepted": True, "value": request["value"]}
    return execute_idempotent(db, principal=principal, operation="create", key=key,
                              request=request, execute=execute)


def test_replay_does_not_repeat_effect(db):
    first = call(db, {"value": "hello"})
    second = call(db, {"value": "hello"}, execute=lambda conn: 1 / 0)
    assert first.response == second.response == {"accepted": True, "value": "hello"}
    assert first.replayed is False and second.replayed is True
    assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1


def test_request_mismatch_rejected_without_effect(db):
    call(db, {"value": "hello"})
    with pytest.raises(IdempotencyConflict):
        call(db, {"value": "different"})
    assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1


def test_callback_failure_rolls_back(db):
    def fail(conn):
        conn.execute("INSERT INTO effects(value) VALUES('partial')")
        raise RuntimeError("failed")
    with pytest.raises(RuntimeError):
        call(db, {"value": "hello"}, execute=fail)
    assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0


def test_unserializable_response_rolls_back(db):
    def bad(conn):
        conn.execute("INSERT INTO effects(value) VALUES('partial')")
        return {"not_json": object()}
    with pytest.raises(TypeError):
        call(db, {"value": "hello"}, execute=bad)
    assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0


def test_principal_scoping_and_canonical_hash(db):
    assert canonical_request_hash({"a": 1, "b": 2}) == canonical_request_hash({"b": 2, "a": 1})
    assert call(db, {"value": "one"}).replayed is False
    assert call(db, {"value": "two"}, principal="bob").replayed is False
    assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 2


def test_nested_transaction_rejected(db):
    db.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="Nested"):
            call(db, {"value": "hello"})
    finally:
        db.execute("ROLLBACK")


def test_concurrent_same_key_executes_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    from pathlib import Path
    from threading import Barrier

    from synapse.persistence.migrations import apply_migrations, open_database

    path = tmp_path / "commands.sqlite"
    with closing(open_database(path)) as connection:
        apply_migrations(connection, Path(__file__).resolve().parents[1] / "migrations")
        connection.execute("CREATE TABLE effects (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    ready = Barrier(2)

    def execute():
        with closing(open_database(path)) as connection:
            ready.wait(timeout=5)
            return call(connection, {"value": "hello"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert sorted(result.replayed for result in results) == [False, True]
    assert [result.response for result in results] == [{"accepted": True, "value": "hello"}] * 2
    with closing(open_database(path)) as connection:
        assert connection.execute("SELECT value FROM effects").fetchall() == [("hello",)]


def test_non_string_request_keys_cannot_collide_with_json_keys(db):
    with pytest.raises(TypeError, match="keys must be strings"):
        call(db, {"value": "hello", "nested": [{1: "x"}]})
    assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
