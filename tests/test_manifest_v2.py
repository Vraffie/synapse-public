"""ADR 026 D2/D8/D9: candidate manifest v2 builder, verifier and store (migration 0019), and the approval binding
for multi-repository runs. Real git in tmp_path and the fake CLI of test_multi_repo_runs only: no real claude or
codex, no network.
"""
import dataclasses
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from synapse.api.openapi_contracts import APPROVAL_SUBJECT
from synapse.application import candidate_manifest as v1
from synapse.application import candidate_manifest_v2 as v2
from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.application.approval_service import Actor, ApprovalAccessDenied, ApprovalService
from synapse.daemon import manifest_builder as mb
from synapse.daemon.manifest_builder import ManifestBuilder, ManifestRefused
from synapse.persistence import candidate_manifests as store
from synapse.persistence.approval_binding import ApprovalNotGranted, DenialReason, verify_approval_for_execution
from synapse.persistence.approval_query import read_approvals
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict

from test_multi_repo_runs import REPO_IDS, ROOT, World, git, head  # noqa: F401  (shared harness)


def request_manifest_approval(builder, requester, *, manifest_sha256, operation, target, policy_version,
                              idempotency_key, correlation_id, ttl=timedelta(hours=1)):
    """What ManifestService.request_approval does, without HTTP or threads: recheck first, then request."""
    manifest = builder.recheck(manifest_sha256)
    return requester.request_approval(run_id=manifest.run_id, operation=operation, target=target,
                                      candidate_sha256=manifest_sha256, policy_version=policy_version,
                                      expires_at=(datetime.now(timezone.utc) + ttl).isoformat(),
                                      idempotency_key=idempotency_key, correlation_id=correlation_id)


A, B, C = "a" * 40, "b" * 40, "c" * 40


# --------------------------------------------------------------------------------------------- pure v2 format
def write_entry(repo_id="portfolio", candidate=B, task="t1", attempt=1, changed=2, diff=b"diff"):
    import hashlib
    return v2.RepoEntry(repo_id, "write", "refs/heads/main", A, candidate_sha=candidate,
                        source=v2.SourceAttempt(task, attempt), diff_sha256=hashlib.sha256(diff).hexdigest(),
                        diff_bytes=len(diff), changed_paths=changed,
                        validations=v2.passed_validations(repo_id, A, candidate, changed))


def read_entry(repo_id="tenant-specification"):
    return v2.RepoEntry(repo_id, "read", "refs/heads/main", C)


def sample():
    return v2.build_manifest_v2("run1", "d" * 64, [read_entry(), write_entry()])


def test_v2_canonical_bytes_round_trip_and_hash():
    m = sample()
    data = m.canonical_bytes()
    assert v2.manifest_v2_from_bytes(data) == m
    assert m.manifest_sha256 == __import__("hashlib").sha256(data).hexdigest()
    body = json.loads(data)
    assert body["v"] == 2 and [r["repo_id"] for r in body["repos"]] == ["portfolio", "tenant-specification"]
    assert set(body["repos"][1]) == {"access", "baseline_ref", "baseline_sha", "repo_id"}  # read: baseline only
    assert body["repos"][0]["source"] == {"attempt_no": 1, "task_id": "t1"}
    # no timestamps, paths or prompts anywhere
    assert not {"created_at", "path", "prompt", "worktree"} & set(json.dumps(body).replace('"', " ").split())


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),
    lambda d: d["repos"][1].update(candidate_sha=B),         # a read entry carries only its baseline
    lambda d: d["repos"][0].pop("source"),
    lambda d: d["repos"][0].update(access="admin"),
    lambda d: d["repos"][0]["validations"].pop(),           # exactly the two v1 validations
    lambda d: d["repos"][0]["validations"][0].update(outcome="skipped"),
    lambda d: d["repos"][0]["validations"][0].update(evidence_sha256="0" * 64),
    lambda d: d["repos"][0].update(changed_paths=True),
    lambda d: d["repos"].reverse(),                          # sorted by repo_id
    lambda d: d.update(v=1),
    lambda d: d["repos"][0].update(baseline_ref="main"),
    lambda d: d["repos"][0].update(repo_id="../x"),
])
def test_v2_strict_parse_refuses(mutate):
    d = json.loads(sample().canonical_bytes())
    mutate(d)
    with pytest.raises(v2.ManifestError):
        v2.manifest_v2_from_bytes(json.dumps(d, sort_keys=True, separators=(",", ":")).encode())


def test_v2_non_canonical_bytes_and_duplicate_keys_refused():
    data = sample().canonical_bytes()
    with pytest.raises(v2.ManifestError):
        v2.manifest_v2_from_bytes(data.replace(b",", b", ", 1))
    with pytest.raises(v2.ManifestError):
        v2.manifest_v2_from_bytes(b'{"v":2,"v":2}')


def test_v2_needs_a_change_and_one_attempt_per_writer_task():
    import hashlib
    unchanged = v2.RepoEntry("portfolio", "write", "refs/heads/main", A, candidate_sha=A,
                             source=v2.SourceAttempt("t1", 1), diff_sha256=hashlib.sha256(b"").hexdigest(),
                             diff_bytes=0, changed_paths=0, validations=v2.passed_validations("portfolio", A, A, 0))
    with pytest.raises(v2.ManifestError, match="no_changes"):
        v2.build_manifest_v2("run1", "d" * 64, [unchanged, read_entry()])
    assert v2.build_manifest_v2("run1", "d" * 64, [unchanged, write_entry("x")]).repos[0].repo_id == "portfolio"
    with pytest.raises(v2.ManifestError, match="one attempt"):
        v2.build_manifest_v2("run1", "d" * 64, [write_entry("p1", attempt=1), write_entry("p2", attempt=2)])
    with pytest.raises(v2.ManifestError, match="empty diff"):
        dataclasses.replace(unchanged, changed_paths=1)
    with pytest.raises(v2.ManifestError):
        v2.build_manifest_v2("run1", "d" * 64, [write_entry("r%02d" % i) for i in range(17)])


# ------------------------------------------------------------------------------------------- end to end (git)
PROMPT = {"mode": "ok", "writes": {"portfolio": {"config.xml": "<p/>\n"}, "product-config": {"deep/x.xml": "<x/>\n"}}}


@pytest.fixture
def built(tmp_path):
    w = World(tmp_path)
    result = w.start(w.request([w.task(prompt=PROMPT)]))
    assert w.states() == (["succeeded"], {"edit": "completed"})
    w.run_id = result.response["run_id"]
    w.task_id = result.response["tasks"][0]["task_id"]
    w.builder = ManifestBuilder(w.db, w.runtime.blobs, w.manager)
    yield w
    w.close()


def test_build_binds_every_repository_and_is_idempotent(built):
    w = built
    out = w.builder.build(w.run_id)
    assert out["seq"] == 1 and out["replayed"] is False and out["invalidated"] == []
    m = w.builder.load(out["manifest_sha256"])
    assert [r.repo_id for r in m.repos] == sorted(REPO_IDS)
    branch = "synapse/%s/%s/1" % (w.run_id, w.task_id)
    by = {r.repo_id: r for r in m.repos}
    for rid in ("portfolio", "product-config"):
        e = by[rid]
        assert e.access == "write" and e.candidate_sha == git(w.repos[rid], "rev-parse", branch).strip()
        assert e.baseline_sha == head(w.repos[rid], "main") and e.changed_paths == 1
        assert e.source == v2.SourceAttempt(w.task_id, 1)
        stored = w.runtime.blobs.read(e.diff_sha256)  # the diff is published and content-addressed
        assert len(stored) == e.diff_bytes and b"diff --git" in stored
    assert by["tenant-specification"].access == "read" and by["tenant-specification"].candidate_sha is None
    assert m.bindings_sha256 == w.db.execute("SELECT DISTINCT bindings_sha256 FROM run_repo_bindings").fetchone()[0]
    # same candidates: same hash, artifact and row
    again = w.builder.build(w.run_id)
    assert (again["manifest_sha256"], again["artifact_id"], again["seq"], again["replayed"]) == \
        (out["manifest_sha256"], out["artifact_id"], 1, True)
    assert w.db.execute("SELECT count(*) FROM candidate_manifests").fetchone() == (1,)
    assert w.builder.recheck(out["manifest_sha256"]) == m
    # v1 and v2 never verify as each other
    with pytest.raises(v1.ManifestError):
        v1.verify_manifest_artifact(w.db, w.runtime.blobs, out["artifact_id"])
    # the manifest carries no path, and nothing in the manifest row or artifact names a file
    raw = w.runtime.blobs.read(out["manifest_sha256"]).decode()
    assert w.repos["portfolio"] not in raw and "deep/x.xml" not in raw and w.root not in raw


def test_baseline_moved_invalidates_every_manifest_and_stops_the_run(built):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    (w.tmp / "repos" / "portfolio" / "config.xml").write_text("<moved/>\n")
    git(w.repos["portfolio"], "commit", "-qam", "owner moves main")
    with pytest.raises(ManifestRefused) as e:
        w.builder.recheck(sha)
    assert e.value.code == "baseline_moved"
    assert store.invalidations(w.db, sha) == ["baseline_moved"]
    with pytest.raises(ManifestRefused, match="manifest_invalidated"):
        w.builder.recheck(sha)
    with pytest.raises(ManifestRefused, match="baseline_moved"):
        w.builder.build(w.run_id)
    # even with the ref moved back, the run stays done: the database refuses a new manifest row
    git(w.repos["portfolio"], "reset", "-q", "--hard", "HEAD^")
    with pytest.raises(ManifestRefused, match="baseline_moved"):
        w.builder.build(w.run_id)


def test_verifier_refuses_tampered_manifests(built):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    m = w.builder.load(sha)
    i = [r.repo_id for r in m.repos].index("portfolio")
    entry = m.repos[i]

    def swap(**kw):
        repos = list(m.repos)
        repos[i] = dataclasses.replace(entry, **kw)
        return dataclasses.replace(m, repos=tuple(repos))
    other = git(w.repos["portfolio"], "rev-parse", "main").strip()
    cases = {
        "candidate_mismatch": swap(candidate_sha=other, diff_sha256=v2.EMPTY_DIFF_SHA256, diff_bytes=0,
                                   changed_paths=0, validations=v2.passed_validations("portfolio", entry.baseline_sha,
                                                                                    other, 0)),
        "no_source_attempt": dataclasses.replace(m, repos=tuple(
            dataclasses.replace(r, source=v2.SourceAttempt(w.task_id, 2)) if r.access == "write" else r
            for r in m.repos)),
        "diff_mismatch": swap(diff_sha256="0" * 64),
        "bindings_mismatch": dataclasses.replace(m, bindings_sha256="0" * 64),
        "repo_set_mismatch": dataclasses.replace(m, repos=tuple(r for r in m.repos if r.repo_id != "tenant-specification")),
    }
    for detail, bad in cases.items():
        with pytest.raises(ManifestRefused) as e:
            mb.verify_manifest_candidates(w.db, w.manager, bad)
        assert (e.value.code, e.value.detail) == ("candidate_invalid", detail), detail


def test_git_rule_is_rerun_on_the_committed_objects(built):
    """A candidate produced any other way (same-uid agent writing objects itself) fails the D3 rule again."""
    w = built
    b = mb.load_run_repos(w.db, w.run_id)["portfolio"]
    repo = w.repos["portfolio"]
    git(repo, "checkout", "-q", "-b", "side", b.baseline_sha)
    (w.tmp / "repos" / "portfolio" / "build.sh").write_text("echo\n")
    git(repo, "add", "build.sh")
    git(repo, "commit", "-qm", "forged")
    forged = head(repo)
    git(repo, "checkout", "-q", "main")
    with pytest.raises(ManifestRefused) as e:
        mb._git_facts(w.manager, b, forged, 60_000)
    assert (e.value.code, e.value.detail) == ("candidate_invalid", "path_not_editable")
    git(repo, "checkout", "-q", "--orphan", "lone")
    git(repo, "commit", "-qm", "unrelated")
    with pytest.raises(ManifestRefused) as e:
        mb._git_facts(w.manager, b, head(repo), 60_000)
    assert e.value.detail == "not_descendant"
    with pytest.raises(ManifestRefused) as e:
        mb._git_facts(w.manager, b, "f" * 40, 60_000)  # a missing object proves nothing: unverifiable
    assert e.value.code == "candidate_unverifiable"


def test_sources_must_be_writer_tasks_and_completed_attempts(built):
    w = built
    with pytest.raises(ManifestRefused, match="not_a_writer"):
        w.builder.build(w.run_id, sources={"someone-else": 1})
    with pytest.raises(ManifestRefused, match="no_source_attempt"):
        w.builder.build(w.run_id, sources={w.task_id: 7})


def test_failed_and_unchanged_runs_build_no_manifest(tmp_path):
    w = World(tmp_path)
    try:
        r1 = w.start(w.request([w.task(prompt={"writes": {"portfolio": {"build.sh": "x"}}})]))
        builder = ManifestBuilder(w.db, w.runtime.blobs, w.manager)
        with pytest.raises(ManifestRefused, match="no_source_attempt"):
            builder.build(r1.response["run_id"])
        r2 = w.start(w.request([w.task(prompt={"mode": "ok"})]), key="k2")
        with pytest.raises(ManifestRefused, match="no_changes"):
            builder.build(r2.response["run_id"])
        assert w.db.execute("SELECT count(*) FROM candidate_manifests").fetchone() == (0,)
    finally:
        w.close()


# --------------------------------------------------------------------------------------- approvals (D9)
class Humans:
    def __init__(self):
        self.actor = Actor("owner", "human")

    @contextmanager
    def authenticated(self, credential):
        if credential != "approver":
            raise ApprovalAccessDenied("denied")
        yield self.actor

    def allows(self, actor, action, resource):
        return True


def _expiry():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _decide(db, sha, approval_id, decision="approved", key="d1"):
    return ApprovalService(db, Humans()).decide(
        "approver", approval_id=approval_id, expected_version=0, decision=decision, operation="deploy",
        target="DEV", candidate_sha256=sha, policy_version="1", idempotency_key=key, correlation_id="c")


def test_deploy_approval_binds_only_a_live_manifest_of_the_run(built):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    requester = DaemonApprovalRequester(w.db)
    common = dict(run_id=w.run_id, operation="deploy", target="DEV", policy_version="1", expires_at=_expiry(),
                  correlation_id="c")
    bindings = w.db.execute("SELECT DISTINCT bindings_sha256 FROM run_repo_bindings").fetchone()[0]
    for bogus in ("0" * 64, bindings):
        with pytest.raises(sqlite3.DatabaseError, match="live candidate manifest"):
            requester.request_approval(candidate_sha256=bogus, idempotency_key="bad-" + bogus[:8], **common)
    ok = request_manifest_approval(w.builder, requester, manifest_sha256=sha, operation="deploy", target="DEV",
                                   policy_version="1", idempotency_key="deploy-1", correlation_id="c")
    approval_id = ok.response["approval_id"]
    # the approver sees the per-repository baseline and candidate commits
    (row,) = [r for r in read_approvals(w.db, read_blob=w.runtime.blobs.read) if r["id"] == approval_id]
    subject = row["subject"]
    jsonschema.validate(subject, APPROVAL_SUBJECT)
    assert subject["kind"] == "candidate_manifest" and subject["manifest_sha256"] == sha
    assert [(r["repo_id"], r["access"]) for r in subject["repos"]] == [
        ("portfolio", "write"), ("product-config", "write"), ("tenant-specification", "read")]
    assert w.repos["portfolio"] not in json.dumps(subject)
    # invalidate after the request: the decision is refused by the service and by the database
    w.builder.invalidate(sha, "operator", actor="owner")
    with pytest.raises(StateConflict, match="manifest_invalidated"):
        _decide(w.db, sha, approval_id)
    with pytest.raises(sqlite3.DatabaseError, match="manifest_invalidated"):
        w.db.execute("UPDATE approval_requests SET state='approved',state_version=1 WHERE id=?", (approval_id,))
    with pytest.raises(sqlite3.DatabaseError, match="live candidate manifest"):
        requester.request_approval(candidate_sha256=sha, idempotency_key="deploy-2", **common)
    with pytest.raises(ManifestRefused, match="manifest_invalidated"):
        request_manifest_approval(w.builder, requester, manifest_sha256=sha, operation="deploy", target="DEV",
                                  policy_version="1", idempotency_key="deploy-3", correlation_id="c")
    assert read_approvals(w.db)[0]["subject"]["invalidated"] == ["operator"]


def test_invalidation_after_approval_denies_execution(built):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    requester = DaemonApprovalRequester(w.db)
    ok = request_manifest_approval(w.builder, requester, manifest_sha256=sha, operation="deploy", target="DEV",
                                   policy_version="1", idempotency_key="deploy-1", correlation_id="c")
    aid = ok.response["approval_id"]
    assert _decide(w.db, sha, aid).response["state"] == "approved"
    args = dict(approval_id=aid, run_id=w.run_id, operation="deploy", target="DEV", candidate_sha256=sha,
                policy_version="1")
    verify_approval_for_execution(w.db, **args)
    w.builder.invalidate(sha, "candidate_invalid")
    with pytest.raises(ApprovalNotGranted) as e:
        verify_approval_for_execution(w.db, **args)
    assert e.value.reason is DenialReason.MANIFEST_INVALIDATED


def test_single_repository_runs_keep_the_legacy_approval_path(tmp_path):
    db = open_database(tmp_path / "db")
    try:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        out = DaemonApprovalRequester(db).request_approval(
            run_id="run", operation="deploy", target="DEV", candidate_sha256="a" * 64, policy_version="1",
            expires_at=_expiry(), idempotency_key="k", correlation_id="c")
        assert out.response["state"] == "requested"
        assert read_approvals(db)[0]["subject"] is None
    finally:
        db.close()


# ------------------------------------------------------------------------------------------- tables (0019)
def test_manifest_tables_are_insert_only_and_validated(built):
    w = built
    out = w.builder.build(w.run_id)
    sha = out["manifest_sha256"]
    rec = store.load_manifest(w.db, sha)
    with pytest.raises(sqlite3.DatabaseError):
        w.db.execute("UPDATE candidate_manifests SET seq=2")
    with pytest.raises(sqlite3.DatabaseError):
        w.db.execute("DELETE FROM candidate_manifests")
    with pytest.raises(sqlite3.DatabaseError):
        w.db.execute("INSERT OR REPLACE INTO candidate_manifests VALUES(?,?,?,?,2,1,'now')",
                     (sha, w.run_id, rec.artifact_id, rec.bindings_sha256))
    other = v1.publish_manifest(w.db, w.runtime.blobs, w.run_id, v1.build_manifest(w.run_id, [v1.RepoCandidate(
        "portfolio", A, B, "e" * 64, 1)]), "op", "v1-key")  # a v1 artifact cannot be indexed as v2
    for values in [("1" * 64, w.run_id, rec.artifact_id, rec.bindings_sha256, 2),  # hash is not the artifact's
                   (other.response["manifest_sha256"], w.run_id, other.response["artifact_id"], rec.bindings_sha256, 2),
                   (sha, w.run_id, rec.artifact_id, "0" * 64, 2)]:
        with pytest.raises(sqlite3.DatabaseError):
            w.db.execute("INSERT INTO candidate_manifests VALUES(?,?,?,?,2,?,'now')", values)
    w.db.execute("BEGIN")
    assert store.invalidate(w.db, sha, "operator", "owner") is True
    assert store.invalidate(w.db, sha, "operator", "owner") is False  # idempotent per reason
    with pytest.raises(ValueError):
        store.invalidate(w.db, sha, "because", "owner")
    w.db.execute("COMMIT")
    for sql in ("UPDATE candidate_manifest_invalidations SET reason='baseline_moved'",
                "DELETE FROM candidate_manifest_invalidations",
                "INSERT OR REPLACE INTO candidate_manifest_invalidations(manifest_sha256,reason,actor,at) "
                "VALUES('%s','operator','x','now')" % sha):
        with pytest.raises(sqlite3.DatabaseError):
            w.db.execute(sql)
    with pytest.raises(sqlite3.DatabaseError):  # an invalidation needs a manifest
        w.db.execute("INSERT INTO candidate_manifest_invalidations(manifest_sha256,reason,actor,at) "
                     "VALUES(?,'operator','x','now')", ("2" * 64,))


def test_migration_0019_upgrade_from_0018_keeps_data(tmp_path):
    db = open_database(tmp_path / "u.db")
    try:
        assert apply_migrations(db, ROOT / "migrations", limit=18)[-1] == 18
        before = [r[1] for r in db.execute("PRAGMA table_info(approval_requests)")]
        db.execute("INSERT INTO audit_entries(principal, action, resource, decision, details_json, created_at) "
                   "VALUES ('op','read','run','allowed','{}','now')")
        assert apply_migrations(db, ROOT / "migrations", limit=19) == [19]
        assert db.execute("SELECT principal FROM audit_entries").fetchall() == [("op",)]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"candidate_manifests", "candidate_manifest_invalidations"} <= tables
        triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert {"approval_multi_repo_binding", "approval_manifest_invalidated"} <= triggers
        assert [r[1] for r in db.execute("PRAGMA table_info(approval_requests)")] == before  # shape unchanged
    finally:
        db.close()


# ------------------------------------------------------------------- security review regressions (manifest v2)
def _move_main(w, text="<moved/>\n"):
    (w.tmp / "repos" / "portfolio" / "config.xml").write_text(text)
    git(w.repos["portfolio"], "commit", "-qam", "owner moves main")


def test_baseline_moving_during_a_build_refuses_it(built, monkeypatch):
    # the race: D2 passed, then the ref moved while the candidates were verified and published
    w = built
    real = mb.publish_manifest_v2

    def publish_then_move(*a, **kw):
        result = real(*a, **kw)
        _move_main(w)
        return result
    monkeypatch.setattr(mb, "publish_manifest_v2", publish_then_move)
    with pytest.raises(ManifestRefused) as e:
        w.builder.build(w.run_id)
    assert e.value.code == "baseline_moved"
    assert store.run_manifests(w.db, w.run_id) == []  # no row indexed for the moved baseline


def test_baseline_moving_during_a_rebuild_invalidates_the_existing_manifest(built, monkeypatch):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    real = mb.publish_manifest_v2

    def publish_then_move(*a, **kw):
        result = real(*a, **kw)
        _move_main(w)
        return result
    monkeypatch.setattr(mb, "publish_manifest_v2", publish_then_move)
    with pytest.raises(ManifestRefused, match="baseline_moved"):
        w.builder.build(w.run_id)
    assert store.invalidations(w.db, sha) == ["baseline_moved"]


def test_baseline_moved_before_any_manifest_records_nothing(built):
    # documented exactly: with no manifest yet there is nothing to invalidate; builds refuse while the ref differs,
    # and a ref moved back to exactly the pinned SHA builds the manifest of the pinned baseline
    w = built
    pinned = head(w.repos["portfolio"], "main")
    _move_main(w)
    for _ in range(2):
        with pytest.raises(ManifestRefused, match="baseline_moved"):
            w.builder.build(w.run_id)
    assert store.run_manifests(w.db, w.run_id) == []
    git(w.repos["portfolio"], "reset", "-q", "--hard", pinned)
    out = w.builder.build(w.run_id)
    assert out["seq"] == 1 and w.builder.load(out["manifest_sha256"]).repos[0].baseline_sha == pinned


def test_decision_refuses_only_recorded_invalidations_and_the_deploy_precheck_catches_a_move(built):
    # documented behaviour: the decision runs no git; the mandatory deploy pre-check (recheck) does
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    aid = request_manifest_approval(w.builder, DaemonApprovalRequester(w.db), manifest_sha256=sha, operation="deploy",
                                   target="DEV", policy_version="1", idempotency_key="k1",
                                   correlation_id="c").response["approval_id"]
    _move_main(w)
    assert _decide(w.db, sha, aid).response["state"] == "approved"  # nothing recorded the move yet
    args = dict(approval_id=aid, run_id=w.run_id, operation="deploy", target="DEV", candidate_sha256=sha,
                policy_version="1")
    with pytest.raises(ManifestRefused, match="baseline_moved"):
        w.builder.recheck(sha, run_id=w.run_id)  # D9 pre-check step 2
    with pytest.raises(ApprovalNotGranted) as e:
        verify_approval_for_execution(w.db, **args)
    assert e.value.reason is DenialReason.MANIFEST_INVALIDATED


def test_same_diff_in_two_runs_publishes_two_scoped_artifacts(built):
    w = built
    first = w.builder.build(w.run_id)
    run2 = w.start(w.request([w.task(prompt=PROMPT)]), key="k2").response["run_id"]
    assert run2 != w.run_id
    second = ManifestBuilder(w.db, w.runtime.blobs, w.manager).build(run2)
    m1, m2 = w.builder.load(first["manifest_sha256"]), w.builder.load(second["manifest_sha256"])
    d1 = {r.repo_id: r.diff_sha256 for r in m1.repos if r.access == "write"}
    assert d1 == {r.repo_id: r.diff_sha256 for r in m2.repos if r.access == "write"}  # same diff bytes
    rows = w.db.execute("SELECT json_extract(provenance_json,'$.run_id'), json_extract(provenance_json,'$.repo_id') "
                        "FROM artifacts WHERE json_extract(provenance_json,'$.kind')=?",
                        (v1.DIFF_PROVENANCE_KIND,)).fetchall()
    assert sorted(rows) == sorted((r, rid) for r in (w.run_id, run2) for rid in d1)
    assert w.builder.build(run2)["replayed"] is True  # still idempotent per run


def test_unexpected_diff_publish_error_is_a_fixed_refusal(built, monkeypatch):
    w = built

    def boom(*a, **kw):
        raise RuntimeError("/secret/path leaked by the store")
    monkeypatch.setattr(mb, "publish_artifact", boom)
    with pytest.raises(ManifestRefused) as e:
        w.builder.build(w.run_id)
    assert (e.value.code, e.value.detail) == ("artifact_publish_failed", "diff") and "secret" not in str(e.value)
    assert store.run_manifests(w.db, w.run_id) == []


def test_diff_too_large_is_refused_at_build_and_at_verify(built):
    w = built
    with pytest.raises(ManifestRefused) as e:
        mb.collect_manifest(w.db, w.manager, w.run_id, max_diff_bytes=16)
    assert (e.value.code, e.value.detail) == ("candidate_invalid", "diff_too_large")
    manifest = w.builder.load(w.builder.build(w.run_id)["manifest_sha256"])
    with pytest.raises(ManifestRefused) as e:
        mb.verify_manifest_candidates(w.db, w.manager, manifest, max_diff_bytes=16)
    assert (e.value.code, e.value.detail) == ("candidate_invalid", "diff_too_large")


def test_invalidation_is_matched_by_manifest_hash_not_by_the_approvals_run(built):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    w.builder.invalidate(sha, "operator", actor="owner")
    # a legacy (single-repository) run can bind any candidate hash; naming the invalidated manifest must not help
    w.db.execute("INSERT INTO workflow_versions VALUES('wf-legacy','1','{}','hash','now')")
    w.db.execute("INSERT INTO runs VALUES('legacy','wf-legacy','1','created',0,'now','now')")
    aid = DaemonApprovalRequester(w.db).request_approval(
        run_id="legacy", operation="deploy", target="DEV", candidate_sha256=sha, policy_version="1",
        expires_at=_expiry(), idempotency_key="legacy-1", correlation_id="c").response["approval_id"]
    assert store.is_invalidated(w.db, sha) is True and store.is_invalidated(w.db, "9" * 64) is False
    with pytest.raises(StateConflict, match="manifest_invalidated"):
        _decide(w.db, sha, aid)
    with pytest.raises(sqlite3.DatabaseError, match="manifest_invalidated"):
        w.db.execute("UPDATE approval_requests SET state='approved',state_version=1 WHERE id=?", (aid,))


def _spawn_gate_event(w, step, n):
    payload = {"tasks": [{"repos": [{"repo_id": "portfolio", "access": "write"}], "approval_step": step}]}
    w.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,"
                 "occurred_at) VALUES(?,?,?,?,?,1,?,'now')",
                 ("ev-gate-%d" % n, "run", w.run_id, w.run_id, "agent.tasks_configured", json.dumps(payload)))


def test_spawn_gate_branch_is_an_allowlist_of_the_spawn_operation(built):
    from synapse.persistence import agent_tasks as at
    w = built
    gate = "5" * 64  # stands in for a multi-repo task's full spec hash
    requester = DaemonApprovalRequester(w.db)

    def request(op, target="repos", policy="1", key=None):
        return requester.request_approval(run_id=w.run_id, operation=op, target=target, candidate_sha256=gate,
                                          policy_version=policy, expires_at=_expiry(),
                                          idempotency_key=key or "gate-" + op, correlation_id="c")
    # deploy and deploy-like names never use the spawn-gate branch, even when an approval_step names them
    for n, op in enumerate(("deploy", "DEPLOY", "Deploy", "deploy:DEV", "deploy.DEV", "agent.run.deploy")):
        _spawn_gate_event(w, {"operation": op, "target": "repos", "policy_version": "1", "candidate_sha256": gate}, n)
        with pytest.raises(sqlite3.DatabaseError, match="live candidate manifest"):
            request(op)
    _spawn_gate_event(w, {"operation": at.MULTI_REPO_SPAWN_OPERATION, "target": "repos", "policy_version": "1",
                          "candidate_sha256": gate}, 99)
    # target or policy differing from the recorded approval_step is refused
    with pytest.raises(sqlite3.DatabaseError, match="live candidate manifest"):
        request(at.MULTI_REPO_SPAWN_OPERATION, target="other", key="t")
    with pytest.raises(sqlite3.DatabaseError, match="live candidate manifest"):
        request(at.MULTI_REPO_SPAWN_OPERATION, policy="2", key="p")
    assert request(at.MULTI_REPO_SPAWN_OPERATION).response["state"] == "requested"


def test_spawn_gate_trigger_and_code_name_the_same_operation(built):
    from synapse.persistence import agent_tasks as at
    w = built
    (sql,) = w.db.execute("SELECT sql FROM sqlite_master WHERE name='approval_multi_repo_binding'").fetchone()
    assert "NEW.operation = '%s'" % at.MULTI_REPO_SPAWN_OPERATION in sql and "<> 'deploy'" not in sql
    # the spec parser refuses a multi-repository approval_step with any other operation
    def spec(op):
        t = {k: v for k, v in w.task(approval_step={"operation": op, "target": "repos", "policy_version": "1"}).items()
             if k != "id"}
        t["permission_mode"] = "acceptEdits"
        return at.parse_task_specs({"edit": t}, operator_tools=("Read", "Edit"), max_timeout=60,
                                   state_dir=str(w.state), bindings_sha256="7" * 64)
    assert spec(at.MULTI_REPO_SPAWN_OPERATION).specs["edit"]["approval_step"]["operation"] == "agent.run"
    for op in ("deploy", "DEPLOY", "deploy:DEV"):
        with pytest.raises(at.AgentSpecError, match="must be 'agent.run'"):
            spec(op)
        with pytest.raises(Exception, match="specification was rejected"):  # the service answers 422
            w.start(w.request([w.task(approval_step={"operation": op, "target": "repos"})]), key="bad-" + op)


def test_decision_refuses_a_target_or_policy_mismatch(built):
    w = built
    sha = w.builder.build(w.run_id)["manifest_sha256"]
    aid = request_manifest_approval(w.builder, DaemonApprovalRequester(w.db), manifest_sha256=sha, operation="deploy",
                                   target="DEV", policy_version="1", idempotency_key="k1",
                                   correlation_id="c").response["approval_id"]
    service = ApprovalService(w.db, Humans())
    base = dict(approval_id=aid, expected_version=0, decision="approved", operation="deploy", target="DEV",
                candidate_sha256=sha, policy_version="1", correlation_id="c")
    for n, change in enumerate(({"target": "PROD"}, {"policy_version": "2"}, {"operation": "DEPLOY"})):
        with pytest.raises(StateConflict, match="binding differs"):
            service.decide("approver", **{**base, **change, "idempotency_key": "m%d" % n})
    assert service.decide("approver", **base, idempotency_key="ok").response["state"] == "approved"
    args = dict(approval_id=aid, run_id=w.run_id, operation="deploy", target="DEV", candidate_sha256=sha,
                policy_version="1")
    for change in ({"target": "PROD"}, {"policy_version": "2"}):
        with pytest.raises(ApprovalNotGranted):
            verify_approval_for_execution(w.db, **{**args, **change})
