from pathlib import Path
import sqlite3

import pytest

from synapse.persistence.migrations import MigrationError, apply_migrations, open_database


ROOT = Path(__file__).resolve().parents[1]


def test_initial_migration_and_idempotent_replay(tmp_path):
    connection = open_database(tmp_path / "synapse.sqlite")
    try:
        assert apply_migrations(connection, ROOT / "migrations") == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
        assert apply_migrations(connection, ROOT / "migrations") == []
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,)]
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"runs", "tasks", "messages", "events", "outbox"} <= tables
    finally:
        connection.close()


def test_failed_migration_rolls_back_schema_and_version(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_bad.sql").write_text(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        "CREATE TABLE must_rollback (id INTEGER);\n"
        "THIS IS NOT SQL;"
    )
    connection = open_database(tmp_path / "failed.sqlite")
    try:
        with pytest.raises(MigrationError):
            apply_migrations(connection, directory)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='must_rollback'"
        ).fetchone() is None
    finally:
        connection.close()


def test_unknown_applied_migration_rejected(tmp_path):
    connection = open_database(tmp_path / "synapse.sqlite")
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (99, '2026-01-01T00:00:00Z')")
        with pytest.raises(MigrationError, match="unknown migrations"):
            apply_migrations(connection, ROOT / "migrations")
    finally:
        connection.close()


def test_failed_upgrade_preserves_existing_data_and_rolls_back_pending_versions(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    initial = ROOT / "migrations" / "0001_initial.sql"
    (directory / initial.name).write_bytes(initial.read_bytes())
    with open_database(tmp_path / "upgrade.sqlite") as connection:
        assert apply_migrations(connection, directory) == [1]
        connection.execute("INSERT INTO audit_entries(principal, action, resource, decision, details_json, created_at) VALUES ('alice', 'read', 'run', 'allowed', '{}', 'now')")
        (directory / "0002_upgrade.sql").write_text(
            "ALTER TABLE audit_entries ADD COLUMN note TEXT;"
            "UPDATE audit_entries SET note = 'value; with semicolon';"
            "CREATE TRIGGER audit_note AFTER INSERT ON audit_entries BEGIN "
            "UPDATE audit_entries SET note = 'new' WHERE sequence = NEW.sequence; END;"
        )
        third = directory / "0003_upgrade.sql"
        third.write_text("/* An embedded transaction command */ COMMIT;")
        with pytest.raises(MigrationError, match="Migration 3"):
            apply_migrations(connection, directory)
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert connection.execute("SELECT principal FROM audit_entries").fetchall() == [("alice",)]
        assert "note" not in {row[1] for row in connection.execute("PRAGMA table_info(audit_entries)")}
        third.write_text("CREATE INDEX audit_principal ON audit_entries(principal);")
        assert apply_migrations(connection, directory) == [2, 3]
        assert connection.execute("SELECT note FROM audit_entries").fetchall() == [("value; with semicolon",)]
    connection.close()


def test_concurrent_migration_reads_history_under_write_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    from threading import Barrier

    path = tmp_path / "concurrent.sqlite"
    with closing(open_database(path)):
        pass
    ready = Barrier(2)

    def migrate():
        with closing(open_database(path)) as connection:
            ready.wait(timeout=5)
            return apply_migrations(connection, ROOT / "migrations")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: migrate(), range(2)))
    assert sorted(results) == [[], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]]


def test_missing_earlier_applied_migration_rejected(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_first.sql").write_text(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
    )
    (directory / "0002_second.sql").write_text("CREATE TABLE second (id INTEGER);")
    connection = open_database(tmp_path / "history.sqlite")
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (2, '2026-01-01T00:00:00Z')")
        with pytest.raises(MigrationError, match="contiguous prefix"):
            apply_migrations(connection, directory)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='second'"
        ).fetchone() is None
    finally:
        connection.close()


def test_gap_in_applied_history_rejected_even_without_lower_pending(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for version in (1, 2, 3):
        (directory / f"{version:04d}_step.sql").write_text(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
            if version == 1 else f"CREATE TABLE step_{version} (id INTEGER);"
        )
    connection = open_database(tmp_path / "gap.sqlite")
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z')")
        connection.execute("INSERT INTO schema_migrations VALUES (3, '2026-01-01T00:00:00Z')")
        with pytest.raises(MigrationError, match="contiguous prefix"):
            apply_migrations(connection, directory)
    finally:
        connection.close()


def test_upgrade_can_add_checked_column_without_weakening_transaction_guard(tmp_path):
    from contextlib import closing

    directory = tmp_path / "migrations"
    directory.mkdir()
    initial = ROOT / "migrations" / "0001_initial.sql"
    (directory / initial.name).write_bytes(initial.read_bytes())
    with closing(open_database(tmp_path / "checked-upgrade.db")) as connection:
        assert apply_migrations(connection, directory) == [1]
        upgrade = directory / "0002_checked.sql"
        upgrade.write_text(
            "ALTER TABLE approval_requests ADD COLUMN state_version INTEGER "
            "NOT NULL DEFAULT 0 CHECK (state_version >= 0);"
        )
        assert apply_migrations(connection, directory) == [2]
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='approval_requests'"
        ).fetchone()[0]
        assert "CHECK (state_version >= 0)" in schema
        (directory / "0003_invalid.sql").write_text("PRAGMA foreign_keys = OFF;")
        with pytest.raises(MigrationError, match="Migration 3"):
            apply_migrations(connection, directory)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,), (2,)]


def test_event_immutability_upgrade_preserves_existing_event(tmp_path):
    from contextlib import closing

    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in ("0001_initial.sql",):
        (directory / name).write_bytes((ROOT / "migrations" / name).read_bytes())
    with closing(open_database(tmp_path / "upgrade.db")) as connection:
        assert apply_migrations(connection, directory) == [1]
        connection.execute(
            "INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) "
            "VALUES('event','task','task','created',1,'{}','now')"
        )
        name = "0002_immutable_events.sql"
        (directory / name).write_bytes((ROOT / "migrations" / name).read_bytes())
        assert apply_migrations(connection, directory) == [2]
        assert connection.execute("SELECT id FROM events").fetchall() == [("event",)]
        with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
            connection.execute("DELETE FROM events WHERE id='event'")


@pytest.mark.parametrize('existing', [False, True])
def test_missing_migration_file_fails_before_any_upgrade_and_recovers_when_restored(tmp_path, existing):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '0001_first.sql').write_text('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL); CREATE TABLE data(value TEXT);')
    second = directory / '0002_second.sql'
    second.write_text("INSERT INTO data VALUES('preserved');")
    with open_database(tmp_path / 'db.sqlite') as conn:
        if existing:
            assert apply_migrations(conn, directory) == [1, 2]
        second.unlink()
        (directory / '0003_third.sql').write_text('CREATE TABLE third(id INTEGER);')
        with pytest.raises(MigrationError, match='Migration files must form a contiguous sequence'):
            apply_migrations(conn, directory)
        assert not conn.in_transaction
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='third'").fetchone() is None
        if existing:
            assert conn.execute('SELECT value FROM data').fetchall() == [('preserved',)]
            assert conn.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall() == [(1,), (2,)]
        else:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone() is None
        second.write_text("INSERT INTO data VALUES('preserved');")
        assert apply_migrations(conn, directory) == ([3] if existing else [1, 2, 3])
        assert conn.execute('SELECT value FROM data').fetchall() == [('preserved',)]


def test_migration_files_must_start_at_one(tmp_path):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '0002_missing_first.sql').write_text('CREATE TABLE unexpected(id INTEGER);')
    with open_database(tmp_path / 'db.sqlite') as conn:
        with pytest.raises(MigrationError, match='contiguous sequence starting at 1'):
            apply_migrations(conn, directory)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='unexpected'").fetchone() is None
