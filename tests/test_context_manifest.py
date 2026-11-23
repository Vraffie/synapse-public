"""Context manifests (ADR 017 D5, record only): canonical hashing, validation, append-only record."""
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from synapse.application.context_manifest import (
    ManifestConflict, ManifestEntry, ManifestError, build_manifest, record_manifest,
)
from synapse.persistence.migrations import apply_migrations, open_database

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
H = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
HEAD = "a" * 40


def entries():
    return [ManifestEntry("spec", "t1", H("spec")), ManifestEntry("artifact", "art-1", H("a"), 10),
            ManifestEntry("file_range", "src/x.py", H("x"), 99, 3, 9), ManifestEntry("requirement", "REQ-1", H("r")),
            ManifestEntry("validation", "pytest", H("v"))]


def test_hash_is_order_independent_and_changes_with_any_input():
    a = build_manifest("t1", repo_head=HEAD, worktree_clean=True, entries=entries())
    b = build_manifest("t1", repo_head=HEAD, worktree_clean=True, entries=list(reversed(entries())))
    assert a.sha256 == b.sha256 and a.document() == b.document()
    assert build_manifest("t1", repo_head="b" * 40, worktree_clean=True, entries=entries()).sha256 != a.sha256
    assert build_manifest("t1", repo_head=HEAD, worktree_clean=False, entries=entries()).sha256 != a.sha256
    assert build_manifest("t1", repo_head=HEAD, worktree_clean=None, entries=entries()).sha256 != a.sha256  # unknown != clean
    assert build_manifest("t2", repo_head=HEAD, worktree_clean=True, entries=entries()).sha256 != a.sha256
    changed = entries()
    changed[2] = ManifestEntry("file_range", "src/x.py", H("x"), 99, 3, 10)
    assert build_manifest("t1", repo_head=HEAD, worktree_clean=True, entries=changed).sha256 != a.sha256
    changed[2] = ManifestEntry("file_range", "src/x.py", H("changed content"), 99, 3, 9)
    assert build_manifest("t1", repo_head=HEAD, worktree_clean=True, entries=changed).sha256 != a.sha256


def test_identical_duplicates_collapse_and_conflicts_are_refused():
    e = ManifestEntry("artifact", "art-1", H("a"), 10)
    assert len(build_manifest("t", repo_head=None, worktree_clean=None, entries=[e, e]).entries) == 1
    with pytest.raises(ManifestError, match="conflicting"):
        build_manifest("t", repo_head=None, worktree_clean=None, entries=[e, ManifestEntry("artifact", "art-1", H("b"), 10)])


@pytest.mark.parametrize("bad", [
    ManifestEntry("bogus", "r", H("a")), ManifestEntry("artifact", "", H("a")), ManifestEntry("artifact", "r", "XYZ"),
    ManifestEntry("artifact", "r", H("a"), -1), ManifestEntry("artifact", "r", H("a"), True),
    ManifestEntry("file_range", "f", H("a")), ManifestEntry("file_range", "f", H("a"), None, 5, 4),
    ManifestEntry("file_range", "f", H("a"), None, 0, 4), ManifestEntry("artifact", "r", H("a"), None, 1, 2),
    ManifestEntry("artifact", "x" * 513, H("a")), ManifestEntry("artifact", "a\x00b", H("a")),
])
def test_invalid_entries_refused(bad):
    with pytest.raises(ManifestError):
        build_manifest("t", repo_head=None, worktree_clean=None, entries=[bad])


@pytest.mark.parametrize("kwargs", [{"repo_head": "abc"}, {"repo_head": "A" * 40}, {"worktree_clean": 1}])
def test_invalid_header_refused(kwargs):
    with pytest.raises(ManifestError):
        build_manifest("t", **{"repo_head": None, "worktree_clean": None, "entries": [], **kwargs})


def test_too_many_entries_refused():
    many = [ManifestEntry("requirement", f"R{i}", H(str(i))) for i in range(1001)]
    with pytest.raises(ManifestError, match="at most"):
        build_manifest("t", repo_head=None, worktree_clean=None, entries=many)


@pytest.fixture
def db(tmp_path):
    from synapse.persistence.workflow_store import create_run, publish_workflow
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, MIGRATIONS)
        publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id": "n", "dependencies": []}],
                         principal="o", idempotency_key="p")
        run = create_run(db, workflow_id="wf", version="1", principal="o", idempotency_key="k").response["run_id"]
        task = db.execute("SELECT id FROM tasks WHERE run_id=?", (run,)).fetchone()[0]
        for n in (1, 2):
            db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,state) VALUES(?,?,?,'running')", (f"att{n}", task, n))
        yield db, run


def _record(db, run, attempt, manifest):
    db.execute("BEGIN IMMEDIATE")
    try:
        return record_manifest(db, run_id=run, attempt_id=attempt, manifest=manifest)
    finally:
        db.execute("COMMIT")


def test_record_requires_a_transaction_and_a_real_attempt(db):
    db, run = db
    manifest = build_manifest("t1", repo_head=HEAD, worktree_clean=None, entries=entries())
    with pytest.raises(RuntimeError, match="transaction"):
        record_manifest(db, run_id=run, attempt_id="att1", manifest=manifest)
    db.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        record_manifest(db, run_id=run, attempt_id="nope", manifest=manifest)
    db.execute("ROLLBACK")
    # The failed insert left neither a manifest nor an event behind.
    assert db.execute("SELECT count(*) FROM context_manifests").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM events WHERE type='context.manifest_recorded'").fetchone()[0] == 0


def test_record_writes_row_event_and_outbox_together_and_is_idempotent(db):
    db, run = db
    manifest = build_manifest("t1", repo_head=HEAD, worktree_clean=None, entries=entries())
    assert _record(db, run, "att1", manifest) is True
    assert _record(db, run, "att1", manifest) is False
    row = db.execute("SELECT manifest_json, manifest_sha256 FROM context_manifests").fetchone()
    assert row[1] == manifest.sha256 == hashlib.sha256(row[0].encode()).hexdigest()
    assert json.loads(row[0]) == manifest.document() and json.loads(row[0])["worktree_clean"] is None
    (payload,) = db.execute("SELECT payload_json FROM events WHERE type='context.manifest_recorded'").fetchone()
    assert json.loads(payload) == {"task_id": "t1", "manifest_sha256": manifest.sha256, "entry_count": 5}
    assert db.execute("SELECT count(*) FROM outbox o JOIN events e ON e.id=o.event_id WHERE e.type='context.manifest_recorded'").fetchone()[0] == 1


def test_a_different_manifest_for_the_same_attempt_conflicts_but_other_attempts_are_fine(db):
    db, run = db
    first = build_manifest("t1", repo_head=HEAD, worktree_clean=None, entries=entries())
    other = build_manifest("t1", repo_head="b" * 40, worktree_clean=None, entries=entries())
    _record(db, run, "att1", first)
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ManifestConflict):
        record_manifest(db, run_id=run, attempt_id="att1", manifest=other)
    db.execute("ROLLBACK")
    assert _record(db, run, "att2", other) is True
