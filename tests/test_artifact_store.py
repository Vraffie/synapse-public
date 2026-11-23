from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database


@pytest.fixture
def setup(tmp_path):
    blobs = tmp_path / "blobs"
    blobs.mkdir(mode=0o700)
    store = ArtifactBlobStore(blobs, max_bytes=100)
    with closing(open_database(tmp_path / "db.sqlite")) as connection:
        apply_migrations(connection, Path(__file__).resolve().parents[1] / "migrations")
        yield connection, store, blobs


def publish(connection, store, data=b"data", *, key="request", provenance=None):
    return publish_artifact(
        connection, store, chunks=[data], artifact_id="artifact",
        media_type="text/plain", provenance=provenance or {"source": "mock"},
        principal="daemon", idempotency_key=key,
    )


def test_publish_replays_metadata_and_event(setup):
    connection, store, _ = setup
    first = publish(connection, store)
    second = publish(connection, store)
    assert not first.replayed and second.replayed
    assert first.response == second.response
    assert store.read(first.response["sha256"]) == b"data"
    assert connection.execute("SELECT storage_path,byte_size FROM artifacts").fetchone() == (first.response["sha256"], 4)
    for table in ("artifacts", "events", "outbox", "idempotency_keys"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)


def test_changed_content_conflicts_without_second_metadata(setup):
    connection, store, _ = setup
    publish(connection, store)
    with pytest.raises(IdempotencyConflict):
        publish(connection, store, b"different")
    assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_outbox_failure_rolls_back_metadata_but_retains_blob(setup):
    connection, store, blobs = setup
    connection.execute("CREATE TRIGGER fail_artifact BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="failed"):
        publish(connection, store)
    for table in ("artifacts", "events", "outbox", "idempotency_keys"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert len(list(blobs.iterdir())) == 1


def test_nested_transaction_and_invalid_provenance_fail_before_publication(setup):
    connection, store, blobs = setup
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="transaction"):
            publish(connection, store)
    finally:
        connection.execute("ROLLBACK")
    with pytest.raises(ValueError):
        publish(connection, store, provenance={"bad": float("nan")})
    assert list(blobs.iterdir()) == []


def test_provenance_snapshot_survives_mutation_during_stream(setup):
    import json

    connection, store, _ = setup
    provenance = {"source": {"name": "original"}}

    def chunks():
        provenance["source"]["name"] = "changed"
        yield b"data"

    publish_artifact(
        connection, store, chunks=chunks(), artifact_id="artifact",
        media_type="text/plain", provenance=provenance,
        principal="daemon", idempotency_key="request",
    )
    stored = json.loads(connection.execute("SELECT provenance_json FROM artifacts").fetchone()[0])
    assert stored == {"source": {"name": "original"}}
    replay = publish(connection, store, provenance={"source": {"name": "original"}})
    assert replay.replayed


def test_concurrent_publish_with_same_key_executes_once(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    connection, store, _ = setup
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)

    def run(_):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            return publish(conn, store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].response == results[1].response
    assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone() == (1,)


def test_changed_provenance_conflicts_without_mutating_metadata(setup):
    connection, store, _ = setup
    publish(connection, store, provenance={"source": "first"})
    with pytest.raises(IdempotencyConflict):
        publish(connection, store, provenance={"source": "second"})
    assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)


def test_duplicate_artifact_id_rolls_back_new_command(setup):
    connection, store, _ = setup
    publish(connection, store)
    with pytest.raises(sqlite3.IntegrityError):
        publish(connection, store, key="different")
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone() == (1,)
