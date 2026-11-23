"""scripts/upgrade-rehearsal.sh really runs: fresh schema and a schema-12 database upgraded to current."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "upgrade-rehearsal.sh"
MIGRATIONS = ROOT / "migrations"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def make_state(tmp_path, upto=None):
    state = tmp_path / "state"
    (state / "artifacts").mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    if upto is None:
        migrations = MIGRATIONS
    else:
        migrations = tmp_path / "old-migrations"
        migrations.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if int(path.name.split("_", 1)[0]) <= upto:
                shutil.copy(path, migrations / path.name)
    with closing(open_database(state / "synapse.sqlite")) as db:
        apply_migrations(db, migrations)
    return state


def run(tmp_path, state, *extra, env_extra=None):
    wrapper = tmp_path / "agentctl-wrapper"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m synapse.cli.__main__ "$@"\n')
    wrapper.chmod(0o700)
    env = {**os.environ, "AGENTCTL": str(wrapper), "PYTHON": sys.executable,
           "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
           **(env_extra or {})}
    return subprocess.run(["bash", str(SCRIPT), str(state), *extra], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=300)


def test_fresh_schema_has_nothing_to_apply(tmp_path):
    state = make_state(tmp_path)
    work = tmp_path / "work"
    result = run(tmp_path, state, str(work))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "none (already current)" in result.stdout
    assert (work / "db.sqlite").is_file() and (work / "blobs").is_dir()
    assert not (work / "scratch").exists()


def test_schema_12_is_upgraded_to_current(tmp_path):
    state = make_state(tmp_path, upto=12)
    current = max(int(p.name.split("_", 1)[0]) for p in MIGRATIONS.glob("*.sql"))
    assert current > 12
    work = tmp_path / "work"
    result = run(tmp_path, state, str(work))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"schema version now {current}" in result.stdout
    # The rollback point is the pre-upgrade backup; the real state is untouched.
    with closing(open_database(state / "synapse.sqlite")) as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 12


def test_existing_work_dir_is_refused(tmp_path):
    state = make_state(tmp_path)
    (tmp_path / "work").mkdir()
    result = run(tmp_path, state, str(tmp_path / "work"))
    assert result.returncode == 2 and "already exists" in result.stderr


def test_default_rollback_point_is_under_xdg_state_not_tmp(tmp_path):
    state = make_state(tmp_path)
    xdg = tmp_path / "xdg"
    result = run(tmp_path, state, env_extra={"XDG_STATE_HOME": str(xdg), "TMPDIR": str(tmp_path / "nowhere")})
    assert result.returncode == 0, result.stdout + result.stderr
    kept = list((xdg / "synapse").glob("upgrade-rehearsal.*"))
    assert len(kept) == 1 and (kept[0] / "db.sqlite").is_file()
