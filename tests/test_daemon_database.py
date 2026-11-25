from pathlib import Path
import os
import sqlite3

import pytest

from synapse.infrastructure.daemon_database import daemon_database
from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning
from synapse.persistence.migrations import MigrationError


ROOT = Path(__file__).resolve().parents[1]


def test_database_lifecycle_holds_lock_and_closes_connection(tmp_path):
    path = tmp_path / "daemon.sqlite"
    with daemon_database(path, ROOT / "migrations") as connection:
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,)]
        assert path.stat().st_mode & 0o077 == 0
        with pytest.raises(DaemonAlreadyRunning):
            with daemon_database(path, ROOT / "migrations"):
                pytest.fail("second daemon acquired database")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with daemon_database(path, ROOT / "migrations") as reopened:
        assert reopened.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,)]


def test_migration_failure_releases_ownership(tmp_path):
    path = tmp_path / "daemon.sqlite"
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_invalid.sql").write_text("THIS IS NOT SQL;")
    with pytest.raises(MigrationError):
        with daemon_database(path, directory):
            pytest.fail("invalid migration succeeded")
    with daemon_database(path, ROOT / "migrations") as connection:
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,)]


def test_rejects_symlink_and_public_database(tmp_path):
    target = tmp_path / "other.sqlite"
    target.touch(mode=0o600)
    symlink = tmp_path / "daemon.sqlite"
    symlink.symlink_to(target)
    with pytest.raises(OSError):
        with daemon_database(symlink, ROOT / "migrations"):
            pytest.fail("symlink accepted")
    target.chmod(0o644)
    with pytest.raises(PermissionError, match="private"):
        with daemon_database(target, ROOT / "migrations"):
            pytest.fail("public database accepted")



def test_rejects_hardlinked_database_alias(tmp_path):
    path = tmp_path / "daemon.sqlite"
    path.touch(mode=0o600)
    os.link(path, tmp_path / "alias.sqlite")
    with pytest.raises(PermissionError, match="one link"):
        with daemon_database(path, ROOT / "migrations"):
            pytest.fail("hardlinked database accepted")
