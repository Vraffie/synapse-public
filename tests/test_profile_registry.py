"""Profile registry: strict loading, legacy compatibility, pure selection and cooldowns (no runtime wiring)."""
import itertools
import json
import os
from pathlib import Path
import random

import pytest

from synapse.daemon.agent_config import AgentConfigError, load_agent_config
from synapse.daemon.profile_registry import (
    LEGACY_PROFILE_ID, Cooldowns, ProfileRegistry, ProfileSpec, RegistryError, load_registry,
    registry_from_single_profile, select_profile, validate_registry,
)


@pytest.fixture
def env(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path, exe, codex, state


def write(tmp_path, data, name="reg.json", mode=0o600, raw=None):
    path = tmp_path / name
    path.write_text(raw if raw is not None else json.dumps(data))
    path.chmod(mode)
    return path


def prof(exe, pid="c1", **over):
    return {"id": pid, "provider": "claude", "executable": str(exe), "allowed_tools": ["Read", "Bash(git status)"],
            "max_concurrency": 2, "enabled": True, **over}


def doc(env, **over):
    tmp, exe, codex, _ = env
    base = {"profiles": [prof(exe, "c1"), prof(exe, "c2", model="opus-5"),
                         {"id": "x1", "provider": "codex", "executable": str(codex), "allowed_tools": [],
                          "max_concurrency": 1, "enabled": True}],
            "primary": "c1", "fallback": ["c2"], "max_concurrency": 4}
    base.update(over)
    return base


# ----------------------------------------------------------------- loading
def test_loads_a_full_registry(env):
    tmp, _, _, state = env
    reg = load_registry(write(tmp, doc(env)), state)
    assert [p.id for p in reg.profiles] == ["c1", "c2", "x1"] and reg.primary == "c1"
    assert reg.fallback == ("c2",) and reg.max_concurrency == 4 and reg.chain == ("c1", "c2")
    assert reg.get("c2").model == "opus-5" and reg.get("x1").provider == "codex" and reg.get("x1").allowed_tools == ()
    assert reg.get("c1").max_timeout_seconds == 3600.0 and reg.get("nope") is None
    assert reg.repos == () and reg.worktree_root is None


def test_repos_and_worktree_root_are_checked_like_the_legacy_loader(env):
    tmp, _, _, state = env
    repo = tmp / "repo"
    (repo / ".git").mkdir(parents=True)
    root = tmp / "wt"
    reg = load_registry(write(tmp, doc(env, repos=[str(repo)], worktree_root=str(root))), state)
    assert reg.repos == (str(repo),) and reg.worktree_root == str(root)
    for bad in ({"repos": [str(repo)]}, {"worktree_root": str(root)},
                {"repos": [str(tmp / "missing")], "worktree_root": str(root)},
                {"repos": [str(repo)], "worktree_root": str(state / "wt")}):
        with pytest.raises(RegistryError):
            load_registry(write(tmp, doc(env, **bad)), state)


def test_legacy_single_profile_file_still_loads_as_a_one_profile_registry(env):
    tmp, exe, _, state = env
    legacy = {"executable": str(exe), "allowed_tools": ["Read", "Edit"], "enabled": True, "max_timeout_seconds": 900}
    path = write(tmp, legacy)
    reg = load_registry(path, state)
    assert reg == registry_from_single_profile(load_agent_config(path, state))
    spec = reg.profiles[0]
    assert (spec.id, spec.provider, spec.max_concurrency, spec.max_timeout_seconds) == (LEGACY_PROFILE_ID, "claude", 2, 900.0)
    assert spec.allowed_tools == ("Read", "Edit") and reg.primary == LEGACY_PROFILE_ID and reg.max_concurrency == 2
    disabled = load_registry(write(tmp, {**legacy, "enabled": False}, "d.json"), state)
    assert disabled.profiles[0].enabled is False  # a disabled legacy profile is still a valid registry
    with pytest.raises(RegistryError):  # legacy rules are enforced unchanged and surface as RegistryError
        load_registry(write(tmp, {**legacy, "enabled": "yes"}, "e.json"), state)
    with pytest.raises(AgentConfigError):  # RegistryError is a startup refusal like AgentConfigError
        load_registry(write(tmp, {**legacy, "extra": 1}, "f.json"), state)


@pytest.mark.parametrize("name,mutate", [
    ("unknown_top_key", lambda d: d.update(surprise=1)),
    ("missing_primary", lambda d: d.pop("primary")),
    ("missing_profiles", lambda d: d.pop("profiles")),
    ("empty_profiles", lambda d: d.update(profiles=[])),
    ("five_profiles", lambda d: d.update(profiles=[{**d["profiles"][0], "id": "p%d" % i} for i in range(5)], primary="p0", fallback=[])),
    ("duplicate_ids", lambda d: d["profiles"].append(dict(d["profiles"][0]))),
    ("unknown_primary", lambda d: d.update(primary="zzz")),
    ("unknown_fallback", lambda d: d.update(fallback=["zzz"])),
    ("fallback_has_primary", lambda d: d.update(fallback=["c1"])),
    ("fallback_duplicate", lambda d: d.update(fallback=["c2", "c2"])),
    ("fallback_not_list", lambda d: d.update(fallback="c2")),
    ("fallback_disabled", lambda d: d["profiles"][1].update(enabled=False)),
    ("bad_provider", lambda d: d["profiles"][0].update(provider="gpt")),
    ("bad_id_chars", lambda d: d["profiles"][0].update(id="../x")),
    ("empty_id", lambda d: d["profiles"][0].update(id="")),
    ("long_id", lambda d: d["profiles"][0].update(id="a" * 65)),
    ("relative_exe", lambda d: d["profiles"][0].update(executable="claude")),
    ("missing_enabled_exe", lambda d: d["profiles"][0].update(executable="/nonexistent/claude")),
    ("claude_without_tools", lambda d: d["profiles"][0].update(allowed_tools=[])),
    ("tools_not_list", lambda d: d["profiles"][0].update(allowed_tools="Read")),
    ("tool_bad_name", lambda d: d["profiles"][0].update(allowed_tools=["rm -rf"])),
    ("tool_duplicate", lambda d: d["profiles"][0].update(allowed_tools=["Read", "Read"])),
    ("cap_zero", lambda d: d["profiles"][0].update(max_concurrency=0)),
    ("cap_seven", lambda d: d["profiles"][0].update(max_concurrency=7)),
    ("cap_bool", lambda d: d["profiles"][0].update(max_concurrency=True)),
    ("cap_float", lambda d: d["profiles"][0].update(max_concurrency=2.0)),
    ("global_cap_seven", lambda d: d.update(max_concurrency=7)),
    ("global_cap_zero", lambda d: d.update(max_concurrency=0)),
    ("enabled_not_bool", lambda d: d["profiles"][0].update(enabled=1)),
    ("timeout_zero", lambda d: d["profiles"][0].update(max_timeout_seconds=0)),
    ("timeout_huge", lambda d: d["profiles"][0].update(max_timeout_seconds=86401)),
    ("timeout_bool", lambda d: d["profiles"][0].update(max_timeout_seconds=True)),
    ("model_bad_chars", lambda d: d["profiles"][0].update(model="opus; rm")),
    ("model_colon", lambda d: d["profiles"][0].update(model="opus:4")),
    ("model_not_string", lambda d: d["profiles"][0].update(model=5)),
    ("profile_unknown_key", lambda d: d["profiles"][0].update(api_key="sk-secret")),
    ("profile_missing_key", lambda d: d["profiles"][0].pop("enabled")),
    ("profile_not_object", lambda d: d.update(profiles=["c1"])),
])
def test_invalid_registries_are_refused(env, name, mutate):
    tmp, _, _, state = env
    d = doc(env)
    mutate(d)
    with pytest.raises(RegistryError):
        load_registry(write(tmp, d), state)


def test_a_credential_field_is_never_accepted(env):
    tmp, _, _, state = env
    for key in ("api_key", "token", "credentials_file", "env"):
        d = doc(env)
        d["profiles"][0][key] = "x"
        with pytest.raises(RegistryError):
            load_registry(write(tmp, d), state)


def test_disabled_profiles_need_no_executable(env):
    tmp, _, _, state = env
    d = doc(env)
    d["profiles"][2].update(enabled=False, executable="/nonexistent/codex")
    d["fallback"] = ["c2"]
    reg = load_registry(write(tmp, d), state)
    assert reg.get("x1").enabled is False


def test_file_safety_rules(env):
    tmp, _, _, state = env
    body = json.dumps(doc(env))
    for mode in (0o666, 0o620, 0o602):
        with pytest.raises(RegistryError, match="writable"):
            load_registry(write(tmp, None, "w%o.json" % mode, mode, raw=body), state)
    real = write(tmp, doc(env), "real.json")
    link = tmp / "link.json"
    link.symlink_to(real)
    with pytest.raises(RegistryError):
        load_registry(link, state)
    with pytest.raises(RegistryError):
        load_registry(tmp / "missing.json", state)
    with pytest.raises(RegistryError):
        load_registry(tmp, state)  # a directory
    fifo = tmp / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(RegistryError):
        load_registry(fifo, state)
    big = write(tmp, None, "big.json", raw=json.dumps({"pad": "x" * (64 * 1024)}))
    with pytest.raises(RegistryError, match="too large"):
        load_registry(big, state)


@pytest.mark.parametrize("raw", [
    '{"profiles": [], "profiles": []}',            # duplicate key
    '{"profiles": [], "primary": NaN}',            # non-finite number
    '{"profiles": [], "primary": Infinity}',
    "not json", "[1]", '"s"', "\xff",
])
def test_malformed_json_is_refused(env, raw):
    tmp, _, _, state = env
    with pytest.raises(RegistryError):
        load_registry(write(tmp, None, raw=raw), state)


def test_duplicate_key_inside_a_profile_is_refused(env):
    tmp, exe, _, state = env
    raw = ('{"profiles": [{"id": "a", "id": "b", "provider": "claude", "executable": "%s", "allowed_tools": ["Read"], '
           '"max_concurrency": 1, "enabled": true}], "primary": "a"}' % exe)
    with pytest.raises(RegistryError):
        load_registry(write(tmp, None, raw=raw), state)


def test_foreign_owned_file_is_refused(env, monkeypatch):
    tmp, _, _, state = env
    path = write(tmp, doc(env))
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(RegistryError, match="user-owned"):
        load_registry(path, state)


@pytest.mark.parametrize("mutate,text", [
    (lambda d: d.update(fallback=["c2", "x1"]), "cannot be a fallback"),
    (lambda d: d.update(primary="x1", fallback=[]), "cannot be the primary"),
])
def test_codex_is_never_primary_or_fallback(env, mutate, text):
    # ADR 016 decision 5: no cross-provider fallback; codex runs only when a task names it.
    tmp, _, _, state = env
    d = doc(env)
    mutate(d)
    with pytest.raises(RegistryError, match=text):
        load_registry(write(tmp, d), state)


def test_validate_registry_rejects_hand_built_inconsistencies(env):
    _, exe, _, _ = env
    spec = ProfileSpec("a", "claude", str(exe), ("Read",), 1, True)
    with pytest.raises(RegistryError):
        validate_registry(ProfileRegistry((spec,), "zzz"))
    with pytest.raises(RegistryError):
        validate_registry(ProfileRegistry((spec,), "a", max_concurrency=9))
    assert validate_registry(ProfileRegistry((spec,), "a")).primary == "a"


# --------------------------------------------------------------- selection
def spec(pid, cap=2, enabled=True, provider="claude"):
    return ProfileSpec(pid, provider, "/bin/true", ("Read",), cap, enabled)


def registry(*specs, primary=None, fallback=(), cap=6):
    return ProfileRegistry(tuple(specs), primary or specs[0].id, tuple(fallback), cap)


def test_primary_is_the_default_and_a_task_may_name_a_profile():
    reg = registry(spec("a"), spec("b"), fallback=("b",))
    d = select_profile(reg, None, {})
    assert (d.profile_id, d.reason, d.fell_back_from, d.skipped) == ("a", "primary", None, ())
    d = select_profile(reg, "b", {})
    assert (d.profile_id, d.reason) == ("b", "requested")
    d = select_profile(reg, "zzz", {})
    assert (d.profile_id, d.reason) == (None, "unknown_profile")


def test_fallback_only_when_the_default_cannot_take_the_task():
    reg = registry(spec("a", cap=1), spec("b"), spec("c"), fallback=("b", "c"))
    assert select_profile(reg, None, {"a": 1}).profile_id == "b"
    d = select_profile(reg, None, {"a": 1})
    assert (d.reason, d.fell_back_from, d.skipped) == ("fallback", "a", (("a", "at_profile_cap"),))
    assert select_profile(reg, None, {"a": 1, "b": 2}).profile_id == "c"
    assert select_profile(reg, None, {}, unavailable={"a"}).skipped == (("a", "unavailable"),)
    assert select_profile(reg, None, {"a": 0}).profile_id == "a"  # capacity free: no fallback


def test_a_disabled_profile_is_never_selected():
    reg = registry(spec("a", enabled=False), spec("b"), fallback=("b",))
    d = select_profile(reg, None, {})
    assert (d.profile_id, d.skipped) == ("b", (("a", "disabled"),))
    only = registry(spec("a", enabled=False))
    d = select_profile(only, None, {})
    assert (d.profile_id, d.reason, d.skipped) == (None, "no_profile_available", (("a", "disabled"),))
    assert select_profile(only, "a", {}).profile_id is None


def test_a_named_profile_does_not_fall_back_unless_the_caller_allows_it():
    reg = registry(spec("a"), spec("b"), spec("c"), fallback=("b", "c"))
    d = select_profile(reg, "c", {"c": 2})
    assert (d.profile_id, d.reason, d.skipped) == (None, "no_profile_available", (("c", "at_profile_cap"),))
    assert select_profile(reg, "c", {}, unavailable={"c"}).profile_id is None
    allowed = select_profile(reg, "c", {"c": 2}, allow_fallback=True)
    assert (allowed.profile_id, allowed.fell_back_from) == ("a", "c")  # chain order, wanted profile first
    assert select_profile(reg, None, {"a": 2}, allow_fallback=False).profile_id is None  # primary default can opt out


def test_global_cap_blocks_everything_and_fallback_cannot_help():
    reg = registry(spec("a", cap=6), spec("b", cap=6), fallback=("b",), cap=3)
    d = select_profile(reg, None, {"a": 2, "b": 1})
    assert (d.profile_id, d.reason, d.skipped) == (None, "at_global_cap", ())
    assert select_profile(reg, None, {"a": 1, "b": 1}).profile_id == "a"
    assert select_profile(reg, "b", {"zzz": 3}).reason == "at_global_cap"  # unknown ids still count (conservative)


def test_load_must_be_non_negative_integers():
    reg = registry(spec("a"))
    for bad in ({"a": -1}, {"a": 1.5}, {"a": True}, {"a": "1"}):
        with pytest.raises(ValueError):
            select_profile(reg, None, bad)


def test_selection_is_pure():
    reg = registry(spec("a", cap=1), spec("b"), fallback=("b",))
    load, blocked = {"a": 1}, {"b"}
    first = select_profile(reg, None, load, blocked)
    assert load == {"a": 1} and blocked == {"b"}
    assert select_profile(reg, None, load, blocked) == first == select_profile(reg, None, dict(load), frozenset(blocked))


def test_selection_never_exceeds_caps_and_is_deterministic_over_random_states():
    rng = random.Random(20261120)
    ids = ["a", "b", "c", "d"]
    for _ in range(3000):
        specs = [spec(i, cap=rng.randint(1, 6), enabled=rng.random() > 0.25) for i in ids[:rng.randint(1, 4)]]
        names = [s.id for s in specs]
        primary = rng.choice(names)
        fallback = tuple(n for n in rng.sample(names, rng.randint(0, len(names))) if n != primary
                         and next(s for s in specs if s.id == n).enabled)
        reg = registry(*specs, primary=primary, fallback=fallback, cap=rng.randint(1, 6))
        load = {n: rng.randint(0, 6) for n in names if rng.random() > 0.3}
        blocked = {n for n in names if rng.random() < 0.25}
        requested = rng.choice([None, *names, "zzz"])
        allow = rng.choice([None, True, False])
        d = select_profile(reg, requested, load, blocked, allow_fallback=allow)
        assert d == select_profile(reg, requested, dict(load), set(blocked), allow_fallback=allow)
        if d.profile_id is None:
            continue
        chosen = reg.get(d.profile_id)
        assert chosen.enabled and d.profile_id not in blocked
        assert load.get(d.profile_id, 0) < chosen.max_concurrency  # per-profile cap
        assert sum(load.values()) < reg.max_concurrency  # global cap: starting one more never exceeds it
        if requested is not None and allow is not True:
            assert d.profile_id == requested  # a named profile never falls back unless allowed
        if d.reason == "fallback":
            assert d.profile_id in reg.chain and d.fell_back_from is not None


def test_exhaustive_small_state_space_never_overcommits():
    reg = registry(spec("a", cap=2), spec("b", cap=1), fallback=("b",), cap=3)
    for la, lb, ua, ub in itertools.product(range(3), range(2), (False, True), (False, True)):
        load = {"a": la, "b": lb}
        blocked = {n for n, flag in (("a", ua), ("b", ub)) if flag}
        d = select_profile(reg, None, load, blocked)
        if d.profile_id:
            after = {**load, d.profile_id: load[d.profile_id] + 1}
            assert after["a"] <= 2 and after["b"] <= 1 and sum(after.values()) <= 3


# ---------------------------------------------------------------- cooldowns
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_cooldown_makes_a_profile_unavailable_until_it_expires():
    clock = Clock()
    cd = Cooldowns(clock)
    assert cd.unavailable() == frozenset() and not cd.active("a") and cd.remaining("a") == 0.0
    cd.set("a", 30)
    assert cd.active("a") and cd.remaining("a") == 30 and cd.unavailable() == {"a"}
    clock.now += 29.5
    assert cd.active("a")
    clock.now += 0.5
    assert not cd.active("a") and cd.unavailable() == frozenset()  # expiry is exact and prunes the mark


def test_cooldown_only_extends_and_can_be_cleared():
    clock = Clock()
    cd = Cooldowns(clock)
    cd.set("a", 60)
    cd.set("a", 10)  # a shorter report never shortens an existing cooldown
    assert cd.remaining("a") == 60
    cd.set("a", 90)
    assert cd.remaining("a") == 90
    cd.clear("a")
    cd.clear("never-set")
    assert not cd.active("a")


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "10", None, 86401])
def test_cooldown_rejects_bad_durations(bad):
    with pytest.raises(ValueError):
        Cooldowns(Clock()).set("a", bad)
    with pytest.raises(ValueError):
        Cooldowns(Clock()).set("", 5)


def test_a_cooling_profile_is_skipped_by_selection_and_returns_afterwards():
    clock = Clock()
    cd = Cooldowns(clock)
    reg = registry(spec("a"), spec("b"), fallback=("b",))
    cd.set("a", 60)
    d = select_profile(reg, None, {}, cd.unavailable())
    assert (d.profile_id, d.skipped) == ("b", (("a", "unavailable"),))
    assert select_profile(reg, "a", {}, cd.unavailable()).profile_id is None  # named profiles do not rotate away
    clock.now += 61
    assert select_profile(reg, None, {}, cd.unavailable()).profile_id == "a"


def test_module_has_no_runtime_dependencies_on_the_engine():
    import synapse.daemon.profile_registry as module
    source = Path(module.__file__).read_text()
    for forbidden in ("agent_runtime", "agent_engine", "agent_service", "subprocess", "sqlite3"):
        assert forbidden not in source
