"""ADR 026 D8 to D10 operator routes for candidate manifests: build, list, show, recheck, deploy approval request.
Real git in tmp_path, the fake CLI of test_multi_repo_runs, the real ManifestService; no real claude, no network."""
import asyncio
import json
import time

import httpx
import pytest

from synapse.api.agent_commands import AgentRunError
from synapse.api.app import create_app
from synapse.application.approval_requests import DaemonApprovalRequester, approval_id_for
from synapse.daemon import manifest_builder as mb
from synapse.daemon.manifest_builder import ManifestBuilder
from synapse.daemon.manifest_service import ManifestService
from synapse.persistence import candidate_manifests as store
from synapse.persistence.approval_query import read_approvals
from synapse.persistence.workflow_store import create_run, publish_workflow

from test_multi_repo_runs import World, git

TOKEN = "m" * 43
PROMPT = {"mode": "ok", "writes": {"portfolio": {"config.xml": "<p/>\n"}, "product-config": {"deep/x.xml": "<x/>\n"}}}
APPROVAL = {"environment": "STAGING"}
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
        assert (out["operation"], out["target"], out["manifest_sha256"], out["state"]) == ("deploy", "STAGING", sha, "requested")
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
        for body in ({}, {"environment": "PRODUCTION"}, {"environment": "STAGING", "operation": "agent.run"},
                     {"environment": "STAGING", "ttl_seconds": 7200}, {"environment": "STAGING", "policy_version": "a b"}):
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


# ------------------------------------------------------------------------------- security review regressions
def post_approval(c, w, sha, key, body):
    return c.post(BASE % w.run_id + "/" + sha + "/approval", headers=headers(key), json=body)


def test_dev_environment_is_refused_until_the_allowlist_has_it(w):
    sha = build(w).json()["manifest_sha256"]

    async def go(c):
        r = await post_approval(c, w, sha, "dev", {"environment": "DEV"})
        assert r.status_code == 422, r.text
    drive(w, go)
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0


def test_service_refuses_an_environment_outside_the_allowlist(w):
    # the service checks the ADR 022 allowlist itself, not only the route schema
    sha = build(w).json()["manifest_sha256"]
    service = ManifestService(w.db, w.builder, DaemonApprovalRequester(w.db))
    with pytest.raises(AgentRunError) as caught:
        asyncio.run(service.request_approval(w.run_id, sha, "DEV", "1", 3600, "dev", "trace"))
    assert (caught.value.status, caught.value.message) == (422, "Invalid approval request")
    assert w.db.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0


def test_replay_of_an_invalidated_manifest_approval_is_refused(w):
    sha = build(w).json()["manifest_sha256"]

    async def ask(c):
        return await post_approval(c, w, sha, "r1", APPROVAL)
    assert drive(w, ask).status_code == 200
    w.builder.invalidate(sha, "candidate_invalid")
    r = drive(w, ask)
    assert (r.status_code, detail(r)) == (409, "manifest_invalidated")
    assert r.headers.get("idempotency-replayed") is None


def test_same_key_with_another_ttl_conflicts(w):
    sha = build(w).json()["manifest_sha256"]

    async def go(c):
        a = await post_approval(c, w, sha, "a1", {"environment": "STAGING", "ttl_seconds": 3600})
        same = await post_approval(c, w, sha, "a1", {"environment": "STAGING", "ttl_seconds": 3600})
        other = await post_approval(c, w, sha, "a1", {"environment": "STAGING", "ttl_seconds": 5})
        default = await post_approval(c, w, sha, "a1", {"environment": "STAGING"})  # default ttl is 3600
        return a, same, other, default
    a, same, other, default = drive(w, go)
    assert a.status_code == 200 and same.status_code == 200 and same.headers["idempotency-replayed"] == "true"
    assert same.json()["expires_at"] == a.json()["expires_at"]
    assert (other.status_code, detail(other)) == (409, "Idempotency key conflicts with prior request")
    assert default.status_code == 200 and default.json()["approval_id"] == a.json()["approval_id"]


def test_manifest_approval_keys_cannot_collide_with_spawn_gate_keys(w):
    sha = build(w).json()["manifest_sha256"]
    key = "gate:" + w.task_id

    async def go(c):
        return await post_approval(c, w, sha, key, APPROVAL)
    r = drive(w, go)
    assert r.status_code == 200, r.text
    approval_id = r.json()["approval_id"]
    assert approval_id == approval_id_for(w.run_id, "manifest:" + key)
    assert approval_id != approval_id_for(w.run_id, key)  # the id the spawn gate of this task would use


def test_sources_are_bounded(w):
    async def go(c):
        out = []
        for sources in ({w.task_id: 2**70}, {w.task_id: 2**31}, {w.task_id: 0}, {w.task_id: -1},
                        {"x" * 129: 1}, {"bad task": 1}, {"": 1}):
            r = await c.post(BASE % w.run_id, headers=headers(), json={"sources": sources})
            out.append((r.status_code, detail(r)))
        ok = await c.post(BASE % w.run_id, headers=headers(), json={"sources": {w.task_id: 2**31 - 1}})
        return out, ok
    out, ok = drive(w, go)
    assert out == [(422, "Invalid request body")] * len(out)
    assert (ok.status_code, detail(ok)) == (422, "no_source_attempt")  # in range, just not an attempt that exists


def test_git_runs_off_the_event_loop(w, monkeypatch):
    sha = build(w).json()["manifest_sha256"]
    original = w.manager.commit_is_ancestor

    def slow(*a, **k):
        time.sleep(1.0)
        return original(*a, **k)
    monkeypatch.setattr(w.manager, "commit_is_ancestor", slow)

    async def go(c):
        ticks = []

        async def ticker():
            start = time.monotonic()
            for _ in range(30):
                await asyncio.sleep(0.05)
                ticks.append(time.monotonic() - start)
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.06)
        r = await c.post(BASE % w.run_id + "/" + sha + "/recheck", headers=headers("rc"))
        await t
        return r, max(b - a for a, b in zip(ticks, ticks[1:]))
    r, gap = drive(w, go)
    assert r.status_code == 200, r.text
    assert gap < 0.5  # a blocked loop would show a gap of about one second


def test_request_deadline_is_503_and_the_lock_serialises(w, monkeypatch):
    sha = build(w).json()["manifest_sha256"]
    original = w.manager.commit_is_ancestor
    running, overlap = [0], [False]

    def slow(*a, **k):
        running[0] += 1
        overlap[0] = overlap[0] or running[0] > 1
        try:
            time.sleep(0.6)
            return original(*a, **k)
        finally:
            running[0] -= 1
    monkeypatch.setattr(w.manager, "commit_is_ancestor", slow)
    service = ManifestService(w.db, w.builder, DaemonApprovalRequester(w.db), deadline_seconds=0.3)

    async def main():
        first = asyncio.create_task(service.recheck(w.run_id, sha))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(service.recheck(w.run_id, sha))
        results = await asyncio.gather(first, second, return_exceptions=True)
        while service._lock.locked():  # the timed-out thread still holds the lock until git returns
            await asyncio.sleep(0.05)
        return results
    results = asyncio.run(main())
    assert [(e.status, e.message) for e in results] == [(503, "candidate_unverifiable:deadline")] * 2
    assert overlap[0] is False
    assert store.invalidations(w.db, sha) == []  # a deadline proves nothing: nothing is recorded
