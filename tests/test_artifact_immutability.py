from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_preserves_artifacts_and_rejects_mutations(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in sorted((ROOT / "migrations").glob("000[1-4]_*.sql")):
        (migrations / source.name).write_bytes(source.read_bytes())
    with closing(open_database(tmp_path / "db")) as connection:
        assert apply_migrations(connection, migrations) == [1, 2, 3, 4]
        values = ("a", "hash", "text/plain", 4, "hash", "{}", "now")
        connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)", values)
        migration = ROOT / "migrations" / "0005_immutable_artifacts.sql"
        (migrations / migration.name).write_bytes(migration.read_bytes())
        assert apply_migrations(connection, migrations) == [5]
        for recursive in (0, 1):
            connection.execute(f"PRAGMA recursive_triggers = {recursive}")
            for sql, args in (
                ("UPDATE artifacts SET sha256='changed' WHERE id='a'", ()),
                ("DELETE FROM artifacts WHERE id='a'", ()),
                ("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)", values),
            ):
                with pytest.raises(sqlite3.IntegrityError, match="artifacts are immutable"):
                    connection.execute(sql, args)
            assert connection.execute("SELECT * FROM artifacts WHERE id='a'").fetchone() == values
        connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                           ("b", "hash", "text/plain", 4, "hash", "{}", "now"))
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (2,)
