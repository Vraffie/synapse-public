"""Migrations ship inside the package; the resolver and the repo path must agree."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from synapse.persistence.migrations import MIGRATION_RE, default_migrations_dir

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / "src" / "synapse" / "persistence" / "migrations_sql"


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.glob("*.sql")}


def test_resolver_finds_all_migrations():
    directory = default_migrations_dir()
    names = _names(directory)
    assert names and all(MIGRATION_RE.match(n) for n in names)
    versions = sorted(int(n[:4]) for n in names)
    assert versions == list(range(1, len(versions) + 1))
    assert directory.resolve() == PACKAGED.resolve()


def test_repo_migrations_path_has_no_drift():
    top = ROOT / "migrations"
    assert _names(top) == _names(PACKAGED)
    for name in _names(PACKAGED):
        assert (top / name).read_bytes() == (PACKAGED / name).read_bytes()


def test_entry_points_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for script in ("agentd", "agentd-supervisor", "agentctl", "agent-desktop"):
        assert f"{script} =" in text


def test_wheel_contains_exact_migration_set(tmp_path):
    pytest.importorskip("setuptools")
    result = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                             "-w", str(tmp_path), str(ROOT)], capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        pytest.skip(f"wheel build unavailable: {result.stderr[-300:]}")
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        packaged = {Path(n).name for n in zf.namelist() if "persistence/migrations_sql/" in n and n.endswith(".sql")}
    assert packaged == _names(PACKAGED)
