"""Candidate manifest: pure hashing/validation, real-git collection and artifact publication/verification."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from synapse.application import candidate_manifest as cm
from synapse.application.candidate_manifest import (
    CandidateManifest, ManifestError, RepoCandidate, ValidationResult, build_manifest, collect_repo_candidate,
    manifest_from_bytes, manifest_from_dict, publish_diff, publish_manifest, verify_manifest_artifact,
)
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore, ArtifactIntegrityError
from synapse.infrastructure.worktrees import WorktreeError, WorktreeManager
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
ENV = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
A, B = "a" * 40, "b" * 40
D1, D2 = "1" * 64, "2" * 64


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


def repo_candidate(**over):
    base = dict(repo_id="catalog", baseline_sha=A, candidate_sha=B, diff_sha256=D1, diff_bytes=10,
                validations=(ValidationResult("lens", "passed", D2), ValidationResult("xml", "skipped")))
    base.update(over)
    return RepoCandidate(**base)


def manifest(**over):
    return build_manifest(over.pop("run_id", "run-1"), over.pop("repos", [repo_candidate()]))


# ---------------------------------------------------------------- pure hashing and validation
def test_hash_is_stable_and_independent_of_input_order_and_key_order():
    r1, r2 = repo_candidate(repo_id="alpha"), repo_candidate(repo_id="beta", candidate_sha="c" * 40)
    m1, m2 = build_manifest("run-1", [r1, r2]), build_manifest("run-1", [r2, r1])
    assert m1 == m2 and m1.manifest_sha256 == m2.manifest_sha256
    shuffled = json.dumps(json.loads(m1.canonical_bytes()), sort_keys=False, indent=2).encode()
    assert shuffled != m1.canonical_bytes()
    assert manifest_from_dict(json.loads(shuffled)).manifest_sha256 == m1.manifest_sha256
    assert m1.canonical_bytes() == json.dumps(json.loads(m1.canonical_bytes()), sort_keys=True,
                                              separators=(",", ":")).encode()
    assert m1.manifest_sha256 == hashlib.sha256(m1.canonical_bytes()).hexdigest()


@pytest.mark.parametrize("field,value", [
    ("repo_id", "other"), ("baseline_sha", "d" * 40), ("candidate_sha", "e" * 40), ("diff_sha256", "3" * 64),
    ("diff_bytes", 11),
    ("validations", (ValidationResult("lens", "failed", D2), ValidationResult("xml", "skipped"))),
    ("validations", (ValidationResult("lens", "passed", "4" * 64), ValidationResult("xml", "skipped"))),
    ("validations", (ValidationResult("lens", "passed", D2),)),
    ("validations", ()),
])
def test_every_field_change_changes_the_hash(field, value):
    assert manifest(repos=[repo_candidate(**{field: value})]).manifest_sha256 != manifest().manifest_sha256


def test_run_id_and_repo_set_are_part_of_the_hash():
    base = manifest().manifest_sha256
    assert manifest(run_id="run-2").manifest_sha256 != base
    assert manifest(repos=[repo_candidate(), repo_candidate(repo_id="zzz")]).manifest_sha256 != base


@pytest.mark.parametrize("bad", [
    dict(baseline_sha="A" * 40), dict(baseline_sha="a" * 39), dict(candidate_sha="g" * 40), dict(candidate_sha=""),
    dict(diff_sha256="1" * 40), dict(diff_sha256="1" * 65), dict(diff_bytes=-1), dict(diff_bytes=True),
    dict(diff_bytes=1.5), dict(repo_id=""), dict(repo_id="../x"), dict(repo_id="a b"), dict(repo_id="x" * 65),
    dict(validations=[ValidationResult("lens", "passed")]),
    dict(validations=(ValidationResult("z", "passed"), ValidationResult("a", "passed"))),
    dict(validations=(ValidationResult("a", "passed"), ValidationResult("a", "failed"))),
    dict(validations=tuple(ValidationResult("v%02d" % i, "passed") for i in range(33))),
])
def test_repo_candidate_rejects_invalid_values(bad):
    with pytest.raises(ManifestError):
        repo_candidate(**bad)


@pytest.mark.parametrize("args", [("lens", "maybe"), ("lens", "passed", "xyz"), ("", "passed"), ("a/b", "passed")])
def test_validation_result_rejects_invalid_values(args):
    with pytest.raises(ManifestError):
        ValidationResult(*args)


def test_manifest_structure_rules():
    with pytest.raises(ManifestError):
        build_manifest("run-1", [])
    with pytest.raises(ManifestError):
        build_manifest("run-1", [repo_candidate(), repo_candidate()])
    with pytest.raises(ManifestError):
        CandidateManifest("run-1", (repo_candidate(repo_id="b"), repo_candidate(repo_id="a")))
    with pytest.raises(ManifestError):
        build_manifest("run-1", [repo_candidate(repo_id="r%02d" % i) for i in range(17)])
    with pytest.raises(ManifestError):
        CandidateManifest("run-1", (repo_candidate(),), version=2)
    with pytest.raises(ValueError):
        build_manifest("", [repo_candidate()])


def test_oversized_manifest_is_rejected():
    many = tuple(ValidationResult("v%02d" % j, "passed", D2) for j in range(32))
    big = [repo_candidate(repo_id="r%02d" % i, validations=many) for i in range(16)]
    with pytest.raises(ManifestError, match="too large"):
        build_manifest("run-1", big)


def test_strict_parsing_rejects_unknown_missing_duplicate_noncanonical_and_nonfinite():
    good = manifest().to_dict()
    assert manifest_from_dict(good) == manifest()
    extra = json.loads(json.dumps(good))
    extra["surprise"] = 1
    with pytest.raises(ManifestError):
        manifest_from_dict(extra)
    missing = json.loads(json.dumps(good))
    del missing["repos"][0]["diff_bytes"]
    with pytest.raises(ManifestError):
        manifest_from_dict(missing)
    nested = json.loads(json.dumps(good))
    nested["repos"][0]["validations"][0]["extra"] = 1
    with pytest.raises(ManifestError):
        manifest_from_dict(nested)
    canon = manifest().canonical_bytes()
    assert manifest_from_bytes(canon) == manifest()
    with pytest.raises(ManifestError, match="canonical"):
        manifest_from_bytes(json.dumps(good, indent=1).encode())
    with pytest.raises(ManifestError, match="duplicate"):
        manifest_from_bytes(canon.replace(b'"run_id":"run-1"', b'"run_id":"run-1","run_id":"run-2"'))
    with pytest.raises(ManifestError):
        manifest_from_bytes(canon.replace(b'"diff_bytes":10', b'"diff_bytes":NaN'))
    with pytest.raises(ManifestError):
        manifest_from_bytes(b"\xff\xfe")
    with pytest.raises(ManifestError):
        manifest_from_bytes(b"[]")
    with pytest.raises(ManifestError):
        manifest_from_bytes(b"x" * (cm.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ManifestError):
        manifest_from_dict({**good, "v": True})


# ---------------------------------------------------------------- real git collection
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "catalog"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "init")
    return str(r.resolve())


@pytest.fixture
def mgr(repo, tmp_path):
    return WorktreeManager([repo], str((tmp_path / "wt").resolve()))


def commit_change(path, name="a.txt", text="two\n", msg="change"):
    Path(path, name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", msg)
    return git(path, "rev-parse", "HEAD")


def test_collect_returns_commits_and_a_hash_bound_diff(mgr, repo):
    wt = mgr.create("run-1", "t1", repo, "main")
    head = commit_change(wt)
    got = collect_repo_candidate(mgr, "run-1", "t1", repo, "main", validations=[ValidationResult("lens", "passed")])
    c = got.candidate
    assert c.repo_id == "catalog" and c.baseline_sha == git(repo, "rev-parse", "main") and c.candidate_sha == head
    assert b"+two" in got.diff and c.diff_bytes == len(got.diff) and c.diff_sha256 == hashlib.sha256(got.diff).hexdigest()
    assert [v.name for v in c.validations] == ["lens"]
    assert collect_repo_candidate(mgr, "run-1", "t1", repo, "main", repo_id="cat").candidate.repo_id == "cat"
    assert git(wt, "status", "--porcelain") == "" and git(wt, "rev-parse", "HEAD") == head  # read-only


def test_unchanged_candidate_is_an_empty_diff(mgr, repo):
    mgr.create("run-1", "t1", repo, "main")
    got = collect_repo_candidate(mgr, "run-1", "t1", repo, "main")
    assert got.candidate.candidate_sha == got.candidate.baseline_sha and got.diff == b""
    assert got.candidate.diff_sha256 == hashlib.sha256(b"").hexdigest()


def test_dirty_and_untracked_trees_are_refused(mgr, repo):
    wt = mgr.create("run-1", "t1", repo, "main")
    commit_change(wt)
    Path(wt, "a.txt").write_text("uncommitted\n")
    with pytest.raises(ManifestError, match="uncommitted"):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "main")
    git(wt, "checkout", "--", "a.txt")
    Path(wt, "new.txt").write_text("untracked\n")
    with pytest.raises(ManifestError, match="untracked"):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "main")


def test_candidate_that_does_not_descend_from_the_baseline_is_refused(mgr, repo):
    wt = mgr.create("run-1", "t1", repo, "main")
    commit_change(wt)
    git(repo, "checkout", "-q", "-b", "other")
    commit_change(repo, "b.txt", "x\n", "elsewhere")
    git(repo, "checkout", "-q", "main")
    with pytest.raises(ManifestError, match="descend"):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "other")


def test_oversized_diff_is_refused_not_truncated(mgr, repo):
    wt = mgr.create("run-1", "t1", repo, "main")
    commit_change(wt, text="x" * 5000 + "\n")
    with pytest.raises(ManifestError, match="size limit"):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "main", max_diff_bytes=1000)
    assert collect_repo_candidate(mgr, "run-1", "t1", repo, "main", max_diff_bytes=100_000).candidate.diff_bytes > 5000


def test_wrong_repo_missing_worktree_unknown_ref_and_bad_limits_are_refused(mgr, repo, tmp_path):
    with pytest.raises(ManifestError):
        collect_repo_candidate(mgr, "run-1", "missing", repo, "main")
    mgr.create("run-1", "t1", repo, "main")
    with pytest.raises(ManifestError):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "no-such-ref")
    with pytest.raises(ManifestError):
        collect_repo_candidate(mgr, "run-1", "t1", "/not/allowlisted", "main")
    with pytest.raises(ManifestError):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "main", max_diff_bytes=0)
    with pytest.raises(ManifestError):
        collect_repo_candidate(mgr, "run-1", "t1", repo, "main", repo_id="../x")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "z").write_text("z")
    git(other, "add", "z")
    git(other, "commit", "-q", "-m", "z")
    two = WorktreeManager([repo, str(other.resolve())], str((tmp_path / "wt2").resolve()))
    two.create("run-2", "t1", str(other.resolve()), "main")
    with pytest.raises(ManifestError, match="belong"):
        collect_repo_candidate(two, "run-2", "t1", repo, "main")


def test_worktree_manager_candidate_helpers_validate_input(mgr, repo):
    mgr.create("run-1", "t1", repo, "main")
    assert mgr.worktree_repo("run-1", "t1") == repo and mgr.worktree_repo("run-1", "nope") is None
    for bad in ("--exec=x", "main", "", "A" * 40, "a" * 39):
        with pytest.raises(ValueError):
            mgr.descends_from("run-1", "t1", bad)
        with pytest.raises(ValueError):
            mgr.diff_since("run-1", "t1", bad)
    with pytest.raises(WorktreeError):
        mgr.descends_from("run-1", "nope", "a" * 40)
    with pytest.raises(WorktreeError):  # an unknown commit is an error (fails closed), not "False"
        mgr.descends_from("run-1", "t1", "f" * 40)
    head = git(mgr.create("run-1", "t1", repo, "main"), "rev-parse", "HEAD")
    assert mgr.descends_from("run-1", "t1", head) is True


# ---------------------------------------------------------------- publication and verification
@pytest.fixture
def store(tmp_path):
    blobs = tmp_path / "blobs"
    blobs.mkdir(mode=0o700)
    with closing(open_database(tmp_path / "db.sqlite")) as db:
        apply_migrations(db, ROOT / "migrations")
        yield db, ArtifactBlobStore(blobs, max_bytes=65536), blobs


def collected(mgr, repo):
    wt = mgr.create("run-1", "t1", repo, "main")
    commit_change(wt)
    return collect_repo_candidate(mgr, "run-1", "t1", repo, "main")


def test_publish_verify_roundtrip_and_bound_hash(store, mgr, repo):
    db, blobs, _ = store
    got = collected(mgr, repo)
    m = build_manifest("run-1", [got.candidate])
    d = publish_diff(db, blobs, "run-1", got, "daemon", "diff-1")
    assert d.response["sha256"] == got.candidate.diff_sha256
    r = publish_manifest(db, blobs, "run-1", m, "daemon", "manifest-1")
    assert r.response["manifest_sha256"] == m.manifest_sha256 and r.response["sha256"] == m.manifest_sha256
    verified = verify_manifest_artifact(db, blobs, r.response["artifact_id"], expected_manifest_sha256=m.manifest_sha256,
                                        run_id="run-1", verify_diffs=True)
    assert verified == m


def test_publish_is_idempotent_and_key_reuse_with_a_different_manifest_conflicts(store):
    db, blobs, _ = store
    m = manifest()
    a = publish_manifest(db, blobs, "run-1", m, "daemon", "k1")
    b = publish_manifest(db, blobs, "run-1", m, "daemon", "k1")
    assert b.replayed and a.response == b.response
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)
    with pytest.raises(IdempotencyConflict):
        publish_manifest(db, blobs, "run-1", manifest(repos=[repo_candidate(diff_bytes=11)]), "daemon", "k1")


def test_publish_refuses_a_manifest_for_another_run_and_a_mismatched_diff(store, mgr, repo):
    db, blobs, _ = store
    with pytest.raises(ManifestError, match="different run"):
        publish_manifest(db, blobs, "run-2", manifest(run_id="run-1"), "daemon", "k")
    got = collected(mgr, repo)
    forged = type(got)(got.candidate, got.diff + b"tampered")
    with pytest.raises(ManifestError, match="does not match"):
        publish_diff(db, blobs, "run-1", forged, "daemon", "d")


def test_blob_tampering_is_detected(store):
    db, blobs, blobdir = store
    r = publish_manifest(db, blobs, "run-1", manifest(), "daemon", "k")
    path = blobdir / r.response["sha256"]
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b"catalog", b"evilcat"))
    with pytest.raises(ArtifactIntegrityError):
        verify_manifest_artifact(db, blobs, r.response["artifact_id"])


def test_binding_mismatches_wrong_kind_and_row_mutation_are_detected(store):
    db, blobs, _ = store
    m = manifest()
    r = publish_manifest(db, blobs, "run-1", m, "daemon", "k")
    aid = r.response["artifact_id"]
    with pytest.raises(ManifestError, match="differs from the hash"):
        verify_manifest_artifact(db, blobs, aid, expected_manifest_sha256=manifest(run_id="run-9").manifest_sha256)
    with pytest.raises(ManifestError, match="different run"):
        verify_manifest_artifact(db, blobs, aid, run_id="run-2")
    with pytest.raises(ManifestError, match="does not exist"):
        verify_manifest_artifact(db, blobs, "nope")
    other = publish_artifact(db, blobs, chunks=[m.canonical_bytes()], artifact_id="plain", media_type="application/json",
                             provenance={"kind": "agent_summary"}, principal="daemon", idempotency_key="plain")
    with pytest.raises(ManifestError, match="not a candidate manifest"):
        verify_manifest_artifact(db, blobs, other.response["artifact_id"])
    with pytest.raises(sqlite3.DatabaseError):  # artifact rows are immutable: provenance cannot be edited
        db.execute("UPDATE artifacts SET provenance_json='{}' WHERE id=?", (aid,))


def test_noncanonical_bytes_published_as_a_manifest_artifact_fail_verification(store):
    db, blobs, _ = store
    m = manifest()
    pretty = json.dumps(m.to_dict(), indent=2).encode()
    forged = publish_artifact(db, blobs, chunks=[pretty], artifact_id="forged", media_type=cm.MEDIA_TYPE,
                              provenance={"kind": cm.PROVENANCE_KIND, "run_id": "run-1",
                                          "manifest_sha256": m.manifest_sha256, "v": 1},
                              principal="daemon", idempotency_key="forged")
    with pytest.raises(ManifestError, match="canonical"):
        verify_manifest_artifact(db, blobs, forged.response["artifact_id"])


def test_verify_diffs_detects_a_missing_diff_blob(store):
    db, blobs, _ = store
    r = publish_manifest(db, blobs, "run-1", manifest(), "daemon", "k")  # the diff blob was never published
    assert verify_manifest_artifact(db, blobs, r.response["artifact_id"]).run_id == "run-1"
    with pytest.raises(ManifestError, match="missing or corrupt"):
        verify_manifest_artifact(db, blobs, r.response["artifact_id"], verify_diffs=True)
