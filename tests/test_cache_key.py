"""Exact-cache key and cacheability rules (M5.4 cache 1/2, ADR 017 D6). Pure module, no storage."""
from dataclasses import replace
import hashlib
import json

import pytest

from synapse.application import cache_key as ck
from synapse.application.cache_key import (
    CacheBypass, CacheHit, CacheKey, CacheKeyInputs, derive_cache_key, may_serve, store_decision,
)
from synapse.application.context_manifest import ManifestEntry, build_manifest

H = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
HEAD = "a" * 40


def manifest(head=HEAD, clean=True, spec="spec", task="t1"):
    return build_manifest(task, repo_head=head, worktree_clean=clean,
                          entries=[ManifestEntry("spec", task, H(spec)), ManifestEntry("artifact", "p1", H("prompt"), 6)])


def inputs(**over):
    base = CacheKeyInputs(
        task_class="validation", operator_marked_cacheable=True, approval_gated=False,
        policy_version="policy-1", approval_policy_sha256=H("approval-policy-1"),
        provider="claude", profile_id="default", cli_version="2.1.0", model="model-a",
        allowed_tools=["Read", "Grep"], permission_mode="plan", settings={"max_turns": 3},
        spec_sha256=H("spec"), manifest=manifest(), validator_sha256s=[H("pytest")])
    return replace(base, **over)


def key(**over) -> CacheKey:
    decision = derive_cache_key(inputs(**over))
    assert isinstance(decision, CacheKey), decision
    return decision


def bypass(**over) -> str:
    decision = derive_cache_key(inputs(**over))
    assert isinstance(decision, CacheBypass), decision
    return decision.reason


def test_same_inputs_same_key_and_set_order_does_not_matter():
    a = key()
    assert key().sha256 == a.sha256
    assert key(allowed_tools=["Grep", "Read", "Read"]).sha256 == a.sha256
    assert key(validator_sha256s=[H("pytest"), H("pytest")]).sha256 == a.sha256
    assert key(settings={"max_turns": 3}).sha256 == a.sha256
    assert len(a.sha256) == 64 and a.material["cache_schema_version"] == ck.CACHE_SCHEMA_VERSION


@pytest.mark.parametrize("change", [
    {"policy_version": "policy-2"},
    {"approval_policy_sha256": H("approval-policy-2")},
    {"provider": "codex"},
    {"profile_id": "other"},
    {"cli_version": "2.1.1"},
    {"model": "model-b"},
    {"model": None},  # unknown model is its own value, never equal to a known one
    {"allowed_tools": ["Read"]},
    {"allowed_tools": ["Read", "Grep", "Glob"]},
    {"permission_mode": "dontAsk"},
    {"settings": {"max_turns": 4}},
    {"settings": {"max_turns": 3, "extra": True}},
    {"spec_sha256": H("spec2")},
    {"manifest": manifest(head="b" * 40)},
    {"manifest": manifest(spec="other spec")},
    {"manifest": manifest(task="t2")},
    {"task_class": "read_only_analysis"},
    {"validator_sha256s": []},
    {"validator_sha256s": [H("ruff")]},
])
def test_every_key_component_change_gives_a_different_key(change):
    assert key(**change).sha256 != key().sha256


def test_schema_version_bump_changes_key(monkeypatch):
    before = key().sha256
    monkeypatch.setattr(ck, "CACHE_SCHEMA_VERSION", 2)
    assert key().sha256 != before


def test_exact_equality_only_no_semantic_normalisation():
    assert key(policy_version="Policy-1").sha256 != key().sha256
    assert key(cli_version="2.1.0 ").sha256 != key().sha256


@pytest.mark.parametrize("task_class,reason", [
    ("edit", "edit_task"), ("review", "review_task"), ("verification", "verification_task"),
    ("independent_check", "independent_check_task"), ("summarise", "unknown_task_class"),
])
def test_never_cacheable_task_classes(task_class, reason):
    assert bypass(task_class=task_class) == reason


def test_edit_and_review_bypass_even_when_operator_marks_them_cacheable():
    for task_class in ("edit", "review"):
        assert bypass(task_class=task_class, operator_marked_cacheable=True).endswith("_task")


@pytest.mark.parametrize("marked", [False, None, 1, "yes"])
def test_default_is_not_cacheable(marked):
    assert bypass(operator_marked_cacheable=marked) == "not_marked_cacheable"


@pytest.mark.parametrize("gated", [True, None, 1])
def test_approval_gated_bypasses(gated):
    assert bypass(approval_gated=gated) == "approval_gated"


@pytest.mark.parametrize("tools,reason", [
    (["Read", "Bash"], "bash_tool"), (["Bash(git status)"], "bash_tool"), (["Bash(*)"], "bash_tool"),
    (["Read", "Edit"], "write_capable_tool"), (["Write"], "write_capable_tool"), (["MultiEdit"], "write_capable_tool"),
    (["NotebookEdit"], "write_capable_tool"), (["WebFetch"], "write_capable_tool"),
    (["Read", "mcp__x__y"], "unknown_tool"), (["Read(src/**)"], "unknown_tool"), (["read"], "unknown_tool"),
])
def test_write_capable_bash_and_unknown_tools_bypass(tools, reason):
    assert bypass(allowed_tools=tools) == reason


@pytest.mark.parametrize("mode,reason", [
    ("acceptEdits", "write_capable_mode"), ("bypassPermissions", "write_capable_mode"), ("auto", "write_capable_mode"),
    ("whatever", "unknown_mode"),
])
def test_write_capable_or_unknown_permission_mode_bypasses(mode, reason):
    assert bypass(permission_mode=mode) == reason


def test_dirty_or_unknown_head_bypasses():
    assert bypass(manifest=manifest(head=None)) == "unknown_head"
    assert bypass(manifest=manifest(clean=None)) == "unknown_cleanliness"
    assert bypass(manifest=manifest(clean=False)) == "dirty_worktree"


def test_unknown_tool_version_bypasses():
    assert bypass(cli_version=None) == "unknown_tool_version"


@pytest.mark.parametrize("outcome", ["unknown", "failed", "cancelled", "timed_out", "weird"])
def test_only_succeeded_attempts_may_be_stored(outcome):
    result = store_decision(key(), outcome=outcome, budget_blocked=False)
    assert isinstance(result, CacheBypass) and result.reason.startswith("outcome_")
    assert store_decision(key(), outcome="succeeded", budget_blocked=False) is None


@pytest.mark.parametrize("blocked", [True, None, 1])
def test_budget_blocked_attempts_are_never_stored(blocked):
    assert store_decision(key(), outcome="succeeded", budget_blocked=blocked) == CacheBypass("budget_blocked")


def test_bypassed_decision_is_never_stored_or_served():
    decision = derive_cache_key(inputs(task_class="edit"))
    assert store_decision(decision, outcome="succeeded", budget_blocked=False) == decision
    assert store_decision("not a decision", outcome="succeeded", budget_blocked=False) == CacheBypass("invalid_input")
    assert not may_serve(decision, CacheHit(key().sha256, "attempt-1"))


def test_hit_is_never_verification_or_approval_and_must_match_exactly():
    k = key()
    hit = CacheHit(k.sha256, "attempt-1")
    assert hit.counts_as_verification is False and hit.satisfies_approval is False and hit.requires_verification is True
    assert may_serve(k, hit)
    assert not may_serve(k, CacheHit(key(policy_version="policy-2").sha256, "attempt-1"))
    assert not may_serve(k, CacheHit(k.sha256, " "))
    assert not may_serve(k, CacheHit(k.sha256.upper(), "attempt-1"))


@pytest.mark.parametrize("change,field", [
    ({"task_class": "x" * 300}, "task_class"), ({"task_class": "valid\x00ation"}, "task_class"),
    ({"policy_version": ""}, "policy_version"), ({"policy_version": "p\n1"}, "policy_version"),
    ({"provider": "p" * 257}, "provider"), ({"cli_version": 2}, "cli_version"),
    ({"allowed_tools": []}, "allowed_tools"), ({"allowed_tools": "Read"}, "allowed_tools"),
    ({"allowed_tools": ["Read"] * 65}, "allowed_tools"), ({"allowed_tools": [None]}, "allowed_tools"),
    ({"spec_sha256": "A" * 64}, "spec_sha256"), ({"approval_policy_sha256": "short"}, "approval_policy_sha256"),
    ({"validator_sha256s": [H("a")] * 65}, "validator_sha256s"), ({"validator_sha256s": ["nothex"]}, "validator_sha256s"),
    ({"validator_sha256s": H("a")}, "validator_sha256s"),
    ({"settings": [1]}, "settings"), ({"settings": {"a": float("nan")}}, "settings"),
    ({"settings": {"a": object()}}, "settings"), ({"settings": {1: "x"}}, "settings"),
    ({"settings": {"a": "x" * (64 * 1024)}}, "settings"),
    ({"manifest": "not a manifest"}, "manifest"),
])
def test_bounded_and_malformed_inputs_bypass_and_never_raise(change, field):
    assert bypass(**change) == f"invalid_input:{field}"


def test_deeply_nested_settings_bypass():
    deep: dict = {"v": 1}
    for _ in range(20):
        deep = {"n": deep}
    assert bypass(settings=deep) == "invalid_input:settings"


def test_non_inputs_object_bypasses():
    assert derive_cache_key("nope") == CacheBypass("invalid_input:inputs")


@pytest.mark.parametrize("name", ["api_key", "ANTHROPIC_API_KEY", "token", "oauth_token", "password", "secret",
                                  "Authorization", "credentials", "session_cookie", "private_key"])
def test_settings_with_credential_named_keys_are_refused(name):
    assert bypass(settings={"env": {name: "sk-live-abc123"}}) == "credential_in_settings"


def test_key_material_holds_only_hashes_ids_and_versions():
    secret = "sk-ant-not-a-real-secret-0123456789"
    k = key(settings={"note": secret, "nested": [{"x": secret}]})
    blob = json.dumps(k.material, sort_keys=True)
    assert secret not in blob and "sk-" not in blob
    # the exact allowlist: no free-text field exists, so prompt text cannot enter the key (only hashes do)
    assert set(k.material) == {
        "cache_schema_version", "policy_version", "approval_policy_sha256", "provider", "profile_id", "cli_version",
        "model", "allowed_tools", "permission_mode", "settings_sha256", "spec_sha256", "manifest_sha256",
        "repo_head", "worktree_clean", "task_class", "validator_sha256s"}
    assert k.material["worktree_clean"] is True and k.material["repo_head"] == HEAD
    # a secret value change still changes the key (it is hashed), but never appears in it
    assert key(settings={"note": secret + "x", "nested": [{"x": secret}]}).sha256 != k.sha256


def test_prompt_only_key_is_impossible():
    assert bypass(spec_sha256=None) == "invalid_input:spec_sha256"
    assert bypass(manifest=None) == "invalid_input:manifest"


def test_module_is_pure_no_storage_or_io():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ck))
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"sqlite3", "os", "pathlib", "subprocess", "socket", "shutil", "io"}
    assert "open(" not in inspect.getsource(ck)


def test_flat_settings_over_node_cap_bypass_before_walking_everything():
    import time
    huge_map = {f"k{i}": i for i in range(2_000_000)}
    huge_list = {"items": list(range(2_000_000))}
    start = time.monotonic()
    assert bypass(settings=huge_map) == "invalid_input:settings_nodes"
    assert bypass(settings=huge_list) == "invalid_input:settings_nodes"
    assert time.monotonic() - start < 1.0


def test_settings_at_node_cap_still_keyed_and_one_over_bypasses():
    n = ck.MAX_SETTINGS_NODES
    assert isinstance(key(settings={f"k{i}": 1 for i in range(n - 1)}), ck.CacheKey)  # root + n-1 leaves
    assert bypass(settings={f"k{i}": 1 for i in range(n)}) == "invalid_input:settings_nodes"


def test_unknown_model_is_a_distinct_key_not_a_bypass():
    # documented tradeoff (ADR 017 D6 "model (if known)"): unknown is keyed apart from every named model
    unknown = key(model=None)
    assert isinstance(unknown, ck.CacheKey) and unknown.material["model"] is None
    assert unknown.sha256 != key(model="model-a").sha256
