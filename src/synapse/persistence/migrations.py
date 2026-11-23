"""Versioned, transactional SQLite schema migrations.

The daemon is the only permitted writer. Apply migrations before starting workers.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
import re
import sqlite3

MIGRATION_RE = re.compile(r"^(?P<version>[0-9]{4})_[a-z0-9_]+\.sql$")


def default_migrations_dir() -> Path:
    """Directory holding the packaged SQL migrations (single source of truth).

    Resolved through importlib.resources so it works from an installed wheel; falls
    back to the repo-relative ``migrations`` path for odd source layouts.
    """
    packaged = Path(str(resources.files("synapse.persistence").joinpath("migrations_sql")))
    if packaged.is_dir() and any(packaged.glob("*.sql")):
        return packaged
    return Path(__file__).resolve().parents[3] / "migrations"


class MigrationError(RuntimeError):
    """Migration could not be applied safely."""


def open_database(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database with integrity and busy-wait defaults."""
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _migration_statements(script: str) -> Iterator[str]:
    start = 0
    for match in re.finditer(";", script):
        end = match.end()
        statement = script[start:end]
        if sqlite3.complete_statement(statement):
            yield statement
            start = end
    if script[start:].strip():
        yield script[start:]


def _authorize_migration(action, arg1, _arg2, _database, _source):
    # SQLite runs quick_check internally when ALTER TABLE adds a CHECK.
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "quick_check":
        return sqlite3.SQLITE_OK
    if action in {
        sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
    }:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def apply_migrations(connection: sqlite3.Connection, directory: Path, *, limit: int | None = None) -> list[int]:
    """Apply pending migrations and their version records in one transaction.

    Migration scripts contain DDL/DML only. Connection settings are owned by
    open_database(). The three legacy PRAGMA lines in migration 0001 are ignored.
    ``limit`` (used to build a schema fingerprint for an older database) stops after that version.
    """
    if connection.in_transaction:
        raise MigrationError("Cannot migrate inside an existing transaction")
    if not directory.is_dir():
        raise MigrationError(f"Migration directory does not exist: {directory}")
    files: dict[int, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_RE.fullmatch(path.name)
        if not match:
            raise MigrationError(f"Invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in files:
            raise MigrationError(f"Duplicate migration version: {version}")
        files[version] = path
    if not files:
        raise MigrationError(f"No migrations in {directory}")
    versions = sorted(files)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("Migration files must form a contiguous sequence starting at 1")

    version = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = (
            {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            if existing else set()
        )
        unknown = applied - files.keys()
        if unknown:
            raise MigrationError(f"Database contains unknown migrations: {sorted(unknown)}")
        pending = sorted(v for v in files.keys() - applied if limit is None or v <= limit)
        if applied and pending and min(pending) < max(applied):
            raise MigrationError("Migration history is not a contiguous prefix")

        for version in pending:
            script = files[version].read_text(encoding="utf-8")
            if files[version].name == "0001_initial.sql":
                script = re.sub(
                    r"^PRAGMA (?:foreign_keys = ON|journal_mode = WAL|busy_timeout = 5000);$",
                    "", script, flags=re.M,
                )
            connection.set_authorizer(_authorize_migration)
            try:
                for statement in _migration_statements(script):
                    connection.execute(statement)
            finally:
                connection.set_authorizer(None)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
        connection.execute("COMMIT")
        return pending
    except BaseException as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if isinstance(exc, (sqlite3.Error, OSError, UnicodeError)):
            context = "Migration transaction" if version is None else f"Migration {version}"
            raise MigrationError(f"{context} failed") from exc
        raise
