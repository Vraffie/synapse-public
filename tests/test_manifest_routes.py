"""ADR 026 D8 to D10 operator routes for candidate manifests: build, list, show, recheck, deploy approval request.
Real git in tmp_path, the fake CLI of test_multi_repo_runs, the real ManifestService; no real claude, no network."""
import asyncio
import json

import httpx
import pytest

from synapse.api.app import create_app
from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.daemon import manifest_builder as mb
from synapse.daemon.manifest_builder import ManifestBuilder
from synapse.daemon.manifest_service import ManifestService
from synapse.persistence import candidate_manifests as store
from synapse.persistence.approval_query import read_approvals
from synapse.persistence.workflow_store import create_run, publish_workflow

from test_multi_repo_runs import World, git

TOKEN = "m" * 43
PROMPT = {"mode": "ok", "writes": {"portfolio": {"config.xml": "<p/>\n"}, "product-config": {"deep/x.xml": "<x/>\n"}}}
APPROVAL = {"environment": "DEV"}
BASE = "/api/v1/runs/%s/candidate-manifests"


def headers(key="k1"):
    return {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": key, "X-Correlation-ID": "trace"}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    result = world.start(world.request([world.task(prompt=PROMPT)]))
    world.run_id = result.response["run_id"]
    world.task_id = result.response["tasks"][0]["task_id"]
    world.builder = ManifestBuilder(world.db, world.runtime.blobs, world.manager)
    yield world
    world.close()


def drive(world, scenario, *, configured=True):
    service = ManifestService(world.db, world.builder if configured else None, DaemonApprovalRequester(world.db))
    app = create_app(token=TOKEN, read_events=lambda *_: [], build_manifest=service.build, list_manifests=service.list,
                     show_manifest=service.show, recheck_manifest=service.recheck,
                     request_manifest_approval=service.request_approval)

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=None), base_url="http://x") as c:
            return await scenario(c)
    return asyncio.run(main())


def build(w):
    async def go(c):
        r = await c.post(BASE % w.run_id, headers=headers(), json={})
        assert r.status_code == 200, r.text
        return r
    return drive(w, go)


def detail(r):
    return r.json()["detail"]


# ------------------------------------------------------------------------------------------------ success
def test_build_list_show_recheck_and_approval(w):
    first = build(w)
    body = first.json()
    sha = body["manifest_sha256"]
    assert body["seq"] == 1 and body["replayed"] is False and body["invalidated"] == []
    assert first.headers["idempotency-replayed"] == "false"

    async def go(c):
        again = await c.post(BASE % w.run_id, headers=headers("k2"), json={})
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert again.json()["manifest_sha256"] == sha and again.headers["idempotency-replayed"] == "true"
        listed = (await c.get(BASE % w.run_id, headers=headers())).json()
        assert listed["run_id"] == w.run_id and len(listed["manifests"]) == 1
        row = listed["manifests"][0]
        assert (row["manifest_sha256"], row["version"], row["seq"], row["valid"], row["invalidations"]) == (sha, 2, 1, True, [])
        shown = await c.get(BASE % w.run_id + "/" + sha, headers=headers())
        assert shown.status_code == 200, shown.text
        view = shown.json()
        assert view["valid"] is True and view["manifest"]["v"] == 2 and view["manifest"]["run_id"] == w.run_id
        assert sorted(d["repo_id"] for d in view["diffs"]) == ["portfolio", "product-config"]
        assert all("diff --git" in d["diff"] and d["truncated"] is False for d in view["diffs"])
        assert w.repos["portfolio"] not in shown.text and w.root not in shown.text  # no host path anywhere
        checked = await c.post(BASE % w.run_id + "/" + sha + "/recheck", headers=headers())
        assert checked.status_code == 200 and checked.json() == {
            "run_id": w.run_id, "manifest_sha256": sha, "valid": True, "invalidations": []}
        asked = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers("deploy-1"), json=APPROVAL)
        assert asked.status_code == 200, asked.text
        out = asked.json()
        assert (out["operation"], out["target"], out["manifest_sha256"], out["state"]) == ("deploy", "DEV", sha, "requested")
        replay = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers("deploy-1"), json=APPROVAL)
        assert replay.json()["approval_id"] == out["approval_id"] and replay.headers["idempotency-replayed"] == "true"
        return out
    out = drive(w, go)
    (approval,) = [a for a in read_approvals(w.db, read_blob=w.runtime.blobs.read) if a["id"] == out["approval_id"]]
    assert approval["subject"]["kind"] == "candidate_manifest" and approval["candidate_manifest_sha256"] == sha
    types = [r[0] for r in w.db.execute("SELECT type FROM events WHERE run_id=? AND type LIKE 'agent.manifest%'", (w.run_id,))]
    assert types.count("agent.manifest_built") == 1 and "agent.manifest_rechecked" in types


# --------------------------------------------------------------------------------------------- unknown run
def test_unknown_run_is_404_on_every_route(w):
    sha = "0" * 64

    async def go(c):
        for method, path, kw in (("POST", BASE % "nope", {"json": {}}), ("GET", BASE % "nope", {}),
                                 ("GET", BASE % "nope" + "/" + sha, {}),
                                 ("POST", BASE % "nope" + "/" + sha + "/recheck", {}),
                                 ("POST", BASE % "nope" + "/" + sha + "/approval", {"json": APPROVAL})):
            r = await c.request(method, path, headers=headers(), **kw)
            assert (r.status_code, r.json()["code"], detail(r)) == (404, "not_found", "Run not found"), (method, path)
    drive(w, go)


def test_unknown_malformed_or_foreign_manifest(w):
    sha = build(w).json()["manifest_sha256"]
    other = create_other_run(w)

    async def go(c):
        for method, tail, kw in (("GET", "", {}), ("POST", "/recheck", {}), ("POST", "/approval", {"json": APPROVAL})):
            for run, digest in ((w.run_id, "1" * 64), (other, sha)):  # unknown hash; a manifest of another run
                r = await c.request(method, BASE % run + "/" + digest + tail, headers=headers(), **kw)
                assert (r.status_code, detail(r)) == (404, "manifest_not_found")
            bad = await c.request(method, BASE % w.run_id + "/" + "G" * 64 + tail, headers=headers(), **kw)
            assert bad.status_code == 422
    drive(w, go)
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0


def create_other_run(w):
    publish_workflow(w.db, workflow_id="plain", version="1", nodes=[{"id": "n", "dependencies": []}],
                     principal="operator", idempotency_key="pub")
    return create_run(w.db, workflow_id="plain", version="1", principal="operator", idempotency_key="run").response["run_id"]


# ------------------------------------------------------------------------------------------------ refusals
def test_build_refusal_codes(w):
    plain = create_other_run(w)

    async def go(c):
        async def post(run, body):
            r = await c.post(BASE % run, headers=headers(), json=body)
            return r.status_code, detail(r)
        assert await post(plain, {}) == (422, "not_multi_repo")
        assert await post(w.run_id, {"sources": {"nobody": 1}}) == (422, "not_a_writer")
        assert await post(w.run_id, {"sources": {w.task_id: 9}}) == (422, "no_source_attempt")
        r = await c.post(BASE % w.run_id, headers=headers(), json={"sources": {w.task_id: "1"}})
        assert r.status_code == 422 and w.task_id not in r.text
        r = await c.post(BASE % w.run_id, headers={k: v for k, v in headers().items() if k != "Idempotency-Key"}, json={})
        assert r.status_code == 400
    drive(w, go)
    assert w.db.execute("SELECT count(*) FROM candidate_manifests").fetchone()[0] == 0


def test_not_configured_daemon_refuses_builds_but_lists(w):
    async def go(c):
        assert (await c.post(BASE % w.run_id, headers=headers(), json={})).status_code == 503
        listed = await c.get(BASE % w.run_id, headers=headers())
        assert listed.status_code == 200 and listed.json()["manifests"] == []
        sha = "0" * 64
        assert (await c.get(BASE % w.run_id + "/" + sha, headers=headers())).status_code == 503
        assert (await c.post(BASE % w.run_id + "/" + sha + "/recheck", headers=headers())).status_code == 503
        assert (await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers(), json=APPROVAL)).status_code == 503
    drive(w, go, configured=False)


def test_approval_body_is_strict(w):
    sha = build(w).json()["manifest_sha256"]

    async def go(c):
        for body in ({}, {"environment": "PRODUCTION"}, {"environment": "DEV", "operation": "agent.run"},
                     {"environment": "DEV", "ttl_seconds": 7200}, {"environment": "DEV", "policy_version": "a b"}):
            r = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers(), json=body)
            assert r.status_code == 422, body
        r = await c.post(BASE % w.run_id + "/" + sha + "/recheck", headers=headers(), json={"x": 1})
        assert r.status_code == 400
    drive(w, go)
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0


# ------------------------------------------------------------------------------------------ invalidation
def test_invalidated_manifest_is_listed_invalid_and_refused(w):
    sha = build(w).json()["manifest_sha256"]
    w.builder.invalidate(sha, "operator", actor="owner")

    async def go(c):
        listed = (await c.get(BASE % w.run_id, headers=headers())).json()["manifests"][0]
        assert listed["valid"] is False and [(i["reason"], i["actor"]) for i in listed["invalidations"]] == [("operator", "owner")]
        shown = (await c.get(BASE % w.run_id + "/" + sha, headers=headers())).json()
        assert shown["valid"] is False and shown["invalidations"][0]["reason"] == "operator"
        for tail, kw in (("/recheck", {}), ("/approval", {"json": APPROVAL})):
            r = await c.post(BASE % w.run_id + "/" + sha + tail, headers=headers(), **kw)
            assert (r.status_code, r.json()["code"], detail(r)) == (409, "conflict", "manifest_invalidated")
    drive(w, go)
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0


def test_candidate_that_no_longer_verifies_is_invalidated_by_recheck(w, monkeypatch):
    sha = build(w).json()["manifest_sha256"]

    def refuse(*_a, **_k):
        raise mb.ManifestRefused("candidate_invalid", "diff_mismatch")
    monkeypatch.setattr(mb, "verify_manifest_candidates", refuse)  # the verifier itself is covered in test_manifest_v2

    async def go(c):
        r = await c.post(BASE % w.run_id + "/" + sha + "/recheck", headers=headers())
        assert (r.status_code, detail(r)) == (409, "candidate_invalid:diff_mismatch")
        listed = (await c.get(BASE % w.run_id, headers=headers())).json()["manifests"][0]
        assert listed["valid"] is False and listed["invalidations"][0]["reason"] == "candidate_invalid"
        r = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers(), json=APPROVAL)
        assert (r.status_code, detail(r)) == (409, "manifest_invalidated")
    drive(w, go)
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0


def test_unverifiable_candidate_is_503_and_records_nothing(w, monkeypatch):
    sha = build(w).json()["manifest_sha256"]

    def unverifiable(*_a, **_k):
        raise mb.ManifestRefused("candidate_unverifiable")
    monkeypatch.setattr(mb, "verify_manifest_candidates", unverifiable)

    async def go(c):
        r = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers(), json=APPROVAL)
        assert (r.status_code, detail(r)) == (503, "candidate_unverifiable")
    drive(w, go)
    assert store.invalidations(w.db, sha) == []


def test_approval_for_a_manifest_whose_baseline_moved_rechecks_first_and_is_refused(w):
    sha = build(w).json()["manifest_sha256"]
    assert store.invalidations(w.db, sha) == []  # nothing recorded yet: only a recheck can notice the move
    (w.tmp / "repos" / "portfolio" / "config.xml").write_text("<moved/>\n")
    git(w.repos["portfolio"], "commit", "-qam", "owner moves main")

    async def go(c):
        r = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers("deploy-1"), json=APPROVAL)
        assert (r.status_code, detail(r)) == (409, "baseline_moved")
        listed = (await c.get(BASE % w.run_id, headers=headers())).json()["manifests"][0]
        assert listed["valid"] is False and listed["invalidations"][0]["reason"] == "baseline_moved"
        again = await c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers("deploy-2"), json=APPROVAL)
        assert (again.status_code, detail(again)) == (409, "manifest_invalidated")
        rebuilt = await c.post(BASE % w.run_id, headers=headers("k3"), json={})
        assert (rebuilt.status_code, detail(rebuilt)) == (409, "baseline_moved")
    drive(w, go)
    assert store.invalidations(w.db, sha) == ["baseline_moved"]
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0
    assert w.db.execute("SELECT count(*) FROM events WHERE type='agent.manifest_refused'").fetchone()[0] >= 2


def test_recheck_of_a_moved_baseline_records_it(w):
    sha = build(w).json()["manifest_sha256"]
    (w.tmp / "repos" / "portfolio" / "config.xml").write_text("<moved/>\n")
    git(w.repos["portfolio"], "commit", "-qam", "owner moves main")

    async def go(c):
        r = await c.post(BASE % w.run_id + "/" + sha + "/recheck", headers=headers())
        assert (r.status_code, detail(r)) == (409, "baseline_moved")
        assert json.dumps(r.json()).count(w.root) == 0
    drive(w, go)
    assert store.invalidations(w.db, sha) == ["baseline_moved"]
