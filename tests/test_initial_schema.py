from pathlib import Path
import sqlite3


def test_initial_schema():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "0001_initial.sql").read_text()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_migrations" in tables
        assert "runs" in tables
        assert "messages" in tables
        assert "profiles" in tables
        assert len(tables) >= 20
    finally:
        conn.close()
