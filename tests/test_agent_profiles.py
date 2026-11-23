"""--agent-config: strict loading, registry, profile sync and service resolution (fake executables only)."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from synapse.api.agent_commands import AgentRunError
from synapse.daemon.agent_config import load_agent_config
from synapse.daemon.agent_profiles import (
    DEFAULT_PROFILE_ID, build_registry, load_agent_profiles, mode_within, sync_profiles,
)
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.daemon.agent_service import AgentRunService
from synapse.daemon.profile_registry import RegistryError
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps({"executable": str(exe), "allowed_tools": ["Read", "Edit", "Bash(git status)"],
                                     "enabled": True, "max_timeout_seconds": 300}))
    base_path.chmod(0o600)
    return tmp_path, state, load_agent_config(base_path, state)


def entry(pid="reader", **over):
    return {"id": pid, "provider": "claude", "allowed_tools": ["Read"], "permission_mode": "plan", "enabled": True, **over}


def write(tmp, profiles, mode=0o600, raw=None):
    path = tmp / "agents.json"
    path.write_text(raw if raw is not None else json.dumps({"profiles": profiles}))
    path.chmod(mode)
    return path


def test_loads_default_plus_declared(env):
    tmp, _, base = env
    reg = load_agent_profiles(write(tmp, [entry(model="sonnet", max_timeout_seconds=60, max_concurrency=2)]), base)
    assert reg.primary == DEFAULT_PROFILE_ID
    r = reg.get("reader")
    assert set(reg.get("default").allowed_tools) == {"Read", "Edit", "Bash(git status)"}
    assert (r.model, r.max_timeout_seconds, r.max_concurrency, r.permission_mode) == ("sonnet", 60.0, 2, "plan")
    assert r.executable == base.profile.executable


@pytest.mark.parametrize("bad,text", [
    (entry(provider="codex"), "codex profile is missing executable"),
    (entry(provider="gemini"), "provider must be 'claude' or 'codex'"),
    (entry(executable="/bin/sh"), "'executable' is refused"),
    (entry(api_key="sk-x"), "credentials do not belong"),
    (entry(id="default"), "reserved"),
    (entry(id="synapse.builtin.x"), "reserved"),
    (entry(id="a" * 65), "id must be"),
    (entry(id="bad id"), "id must be"),
    (entry(allowed_tools=["Write"]), "subset"),
    (entry(allowed_tools=[]), "allowed_tools"),
    (entry(permission_mode="bypassPermissions"), "permission_mode"),
    (entry(max_timeout_seconds=301), "max_timeout_seconds"),
    (entry(max_timeout_seconds=True), "max_timeout_seconds"),
    (entry(model="no spaces"), "model"),
    (entry(max_concurrency=7), "max_concurrency"),
    (entry(enabled="yes"), "enabled"),
    ({"id": "x"}, "missing"),
])
def test_rejects_invalid(env, bad, text):
    tmp, _, base = env
    with pytest.raises(RegistryError, match=text):
        load_agent_profiles(write(tmp, [bad]), base)


def test_rejects_structure_and_files(env):
    tmp, _, base = env
    with pytest.raises(RegistryError, match="exactly the key"):
        load_agent_profiles(write(tmp, None, raw=json.dumps({"profiles": [entry()], "extra": 1})), base)
    with pytest.raises(RegistryError, match="1 to 3"):
        load_agent_profiles(write(tmp, [entry(str(i)) for i in range(4)]), base)
    with pytest.raises(RegistryError, match="unique"):
        load_agent_profiles(write(tmp, [entry(), entry()]), base)
    with pytest.raises(RegistryError, match="duplicate"):
        load_agent_profiles(write(tmp, None, raw='{"profiles": [], "profiles": []}'), base)
    for mode in (0o664, 0o622):
        with pytest.raises(RegistryError, match="writable"):
            load_agent_profiles(write(tmp, [entry()], mode=mode), base)
    with pytest.raises(RegistryError, match="cannot open"):
        load_agent_profiles(tmp / "missing.json", base)
    link = tmp / "link.json"
    link.symlink_to(write(tmp, [entry()]))
    with pytest.raises(RegistryError, match="cannot open"):
        load_agent_profiles(link, base)


def migrated(tmp):
    db = open_database(tmp / "db")
    apply_migrations(db, ROOT / "migrations")
    return db


def test_sync_is_idempotent_versioned_and_disables_removed(env):
    tmp, _, base = env
    db = migrated(tmp)
    reg = load_agent_profiles(write(tmp, [entry(), entry("writer", allowed_tools=["Read", "Edit"],
                                                         permission_mode="acceptEdits")]), base)
    sync_profiles(db, reg)
    sync_profiles(db, reg)
    rows = {r[0]: r for r in db.execute("SELECT id,provider,enabled,max_concurrency,version,config_ref FROM profiles")}
    assert set(rows) == {"reader", "writer"} and rows["reader"][4] == 1 and rows["reader"][5].startswith("registry:")
    sync_profiles(db, load_agent_profiles(write(tmp, [entry(enabled=False)]), base))
    after = {r[0]: r for r in db.execute("SELECT id,enabled,version FROM profiles")}
    assert after["reader"][1:] == (0, 2) and after["writer"][1:] == (0, 2)
    assert db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 2  # never deleted


def test_sync_refuses_to_take_over_a_foreign_row(env):
    tmp, _, base = env
    db = migrated(tmp)
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('reader','claude','x','builtin:x','t')")
    with pytest.raises(RegistryError, match="collides"):
        sync_profiles(db, load_agent_profiles(write(tmp, [entry()]), base))
    assert db.execute("SELECT config_ref FROM profiles").fetchone()[0] == "builtin:x"


def make_service(env, with_file, sub):
    tmp, _, base = env
    work = tmp / sub
    work.mkdir()
    db = migrated(work)
    blobs = work / "blobs"
    blobs.mkdir(mode=0o700)
    runtime = AgentRuntime(db, blobs, profile=base.profile)
    reg = load_agent_profiles(write(tmp, [entry(), entry("off", enabled=False)]), base) if with_file else None
    return AgentRunService(db, runtime, None, reg)


def body(**over):
    return {"repo": "/r", "base_ref": "main", "prompt": "p", "allowed_tools": ["Read"], "permission_mode": "plan",
            "timeout_seconds": 20, **over}


@pytest.mark.parametrize("with_file", [False, True])
def test_service_default_profile_is_backward_compatible(env, with_file):
    svc = make_service(env, with_file, "s")
    out = svc.validate(body(allowed_tools=["Read", "Edit"], permission_mode="acceptEdits", timeout_seconds=300))
    assert out["legacy"] and out["tasks"][0]["permission_mode"] == "acceptEdits"
    assert svc.resolve_profile(None).id == svc.resolve_profile("default").id == "default"
    tasks = {"repo": "/r", "base_ref": "main", "timeout_seconds": 20, "tasks": [{"id": "a", "prompt": "p", "allowed_tools": ["Read"]}]}
    assert svc.validate(tasks)["tasks"][0]["permission_mode"] == "acceptEdits"  # unchanged default


def test_service_resolves_declared_profile_ceilings(env):
    svc = make_service(env, True, "s")
    assert svc.validate(body(), "reader")["tasks"][0]["allowed_tools"] == ["Read"]
    for kw, text in [(dict(allowed_tools=["Edit"]), "subset"), (dict(permission_mode="acceptEdits"), "exceeds"),
                     (dict(timeout_seconds=500), "timeout")]:
        with pytest.raises(AgentRunError, match=text):
            svc.validate(body(**kw), "reader")
    with pytest.raises(AgentRunError, match="unknown profile"):
        svc.validate(body(), "nope")
    with pytest.raises(AgentRunError, match="disabled"):
        svc.validate(body(), "off")
    tasks = {"repo": "/r", "base_ref": "main", "timeout_seconds": 20, "tasks": [{"id": "a", "prompt": "p", "allowed_tools": ["Read"]}]}
    assert svc.validate(tasks, "reader")["tasks"][0]["permission_mode"] == "plan"  # profile mode is the default


def test_profile_field_in_request_is_validated_and_fingerprinted(env):
    svc = make_service(env, True, "s")
    named = svc.validate({**body(), "profile": "reader"})
    assert named["profile"] == "reader"
    with pytest.raises(AgentRunError, match="exceeds"):
        svc.validate({**body(permission_mode="acceptEdits"), "profile": "reader"})
    with pytest.raises(AgentRunError, match="unknown profile"):
        svc.validate({**body(), "profile": "nope"})
    # No profile and an explicit "default" keep the old fingerprint, so earlier receipts still replay.
    plain = svc.validate(body())
    explicit_default = svc.validate({**body(), "profile": "default"})
    assert plain["profile"] is None and explicit_default["profile"] is None
    assert svc._fingerprint(plain) == svc._fingerprint(explicit_default)
    assert "profile" not in svc._fingerprint(plain)
    # A named profile is part of what idempotency compares.
    assert svc._fingerprint(named)["profile"] == "reader"
    assert svc._fingerprint(named) != svc._fingerprint(svc.validate(body()))


def test_service_without_file_knows_only_default(env):
    svc = make_service(env, False, "s")
    with pytest.raises(AgentRunError, match="unknown profile"):
        svc.resolve_profile("reader")


def test_mode_ordering_and_default_registry(env):
    assert mode_within("plan", "acceptEdits") and mode_within("acceptEdits", "acceptEdits")
    assert not mode_within("dontAsk", "acceptEdits") and mode_within("anything", None)
    assert [p.id for p in build_registry(env[2]).profiles] == ["default"]


def run_daemon(state, *args):
    return subprocess.run([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state), *args],
                          capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})


def test_daemon_requires_base_config_and_rejects_an_invalid_codex_entry(env):
    tmp, state, _ = env
    r = run_daemon(state, "--agent-config", str(write(tmp, [entry()])))
    assert r.returncode != 0 and "--claude-cli-config" in r.stderr
    # A codex entry that cannot be mapped onto a codex sandbox (Bash pattern) stops the daemon before any state.
    codex = write(tmp, [entry(provider="codex", executable="/bin/sh", allowed_tools=["Bash(git status)"])])
    r = run_daemon(state, "--claude-cli-config", str(tmp / "base.json"), "--agent-config", str(codex),
                   "--allow-same-user-agents")
    assert r.returncode != 0 and "subset of Edit, Glob, Grep, Read, Write" in r.stderr


def _tasks(*tasks, **over):
    return {"repo": "/r", "base_ref": "main", "timeout_seconds": 20, "tasks": list(tasks), **over}


def test_task_level_profile_is_checked_against_that_profile_not_the_run_default(env):
    svc = make_service(env, True, "s")
    ok = svc.validate(_tasks({"id": "a", "prompt": "p", "allowed_tools": ["Read"], "profile": "reader"},
                             {"id": "b", "prompt": "p", "allowed_tools": ["Read", "Edit"]}))
    a, b = ok["tasks"]
    assert a["profile"] == "reader" and a["permission_mode"] == "plan"  # the profile's mode is its default
    assert "profile" not in b and b["permission_mode"] == "acceptEdits"  # the default profile is unchanged
    for task, text in [({"allowed_tools": ["Edit"]}, "subset"), ({"permission_mode": "acceptEdits"}, "exceeds"),
                       ({"timeout_seconds": 500}, "timeout"), ({"profile": "nope"}, "unknown profile"),
                       ({"profile": "off"}, "disabled")]:
        entry_ = {"id": "a", "prompt": "p", "allowed_tools": ["Read"], "profile": "reader", **task}
        with pytest.raises(AgentRunError, match=text):
            svc.validate(_tasks(entry_))


def test_task_profile_overrides_the_run_level_profile_and_default_is_explicit(env):
    svc = make_service(env, True, "s")
    out = svc.validate(_tasks({"id": "a", "prompt": "p", "allowed_tools": ["Read", "Edit"], "profile": "default"},
                              {"id": "b", "prompt": "p", "allowed_tools": ["Read"]}, profile="reader"))
    a, b = out["tasks"]
    assert a["profile"] == "default" and a["permission_mode"] == "acceptEdits" and "profile" not in b
    assert out["profile"] == "reader"


def test_task_profile_only_changes_the_fingerprint_when_named(env):
    svc = make_service(env, True, "s")
    plain = svc.validate(_tasks({"id": "a", "prompt": "p", "allowed_tools": ["Read"]}))
    named = svc.validate(_tasks({"id": "a", "prompt": "p", "allowed_tools": ["Read"], "profile": "reader"}))
    assert "profile" not in svc._fingerprint(plain)["tasks"][0]  # earlier receipts still replay
    assert svc._fingerprint(named)["tasks"][0]["profile"] == "reader"
    assert svc._fingerprint(named) != svc._fingerprint(plain)


def _events(svc, kind):
    import json as _json
    return [_json.loads(r[0]) for r in svc.db.execute("SELECT payload_json FROM events WHERE type=? ORDER BY sequence", (kind,))]


def test_list_profiles_shows_state_without_paths_or_credentials(env):
    svc = make_service(env, True, "s")
    listed = svc.list_profiles()["profiles"]
    by_id = {p["id"]: p for p in listed}
    assert set(by_id) == {"default", "reader", "off"}
    assert by_id["reader"]["enabled"] and not by_id["off"]["enabled"] and by_id["reader"]["load"] == 0
    assert by_id["default"]["position"] == "primary" and by_id["reader"]["position"] is None
    assert by_id["default"]["load"] is None
    flat = repr(listed)
    assert "/" not in flat and "executable" not in flat and "credentials" not in flat.lower()
    assert [p["id"] for p in make_service(env, False, "s2").list_profiles()["profiles"]] == ["default"]


def test_cooldown_set_and_clear_are_audited_and_validated(env):
    svc = make_service(env, True, "s")
    assert svc.set_cooldown("reader", 60)["cooldown_remaining_seconds"] > 59
    assert {p["id"]: p for p in svc.list_profiles()["profiles"]}["reader"]["cooldown_remaining_seconds"] > 59
    (event,) = _events(svc, "profile.cooldown_set")
    assert event["profile_id"] == "reader" and event["seconds"] == 60 and event["principal"]
    svc.set_cooldown("reader", 10)  # a shorter request does not shorten the cooldown; the audit says so
    assert _events(svc, "profile.cooldown_set")[-1]["seconds"] == 10
    assert _events(svc, "profile.cooldown_set")[-1]["effective_seconds"] > 50
    assert svc.clear_cooldown("reader")["cooldown_remaining_seconds"] == 0.0
    assert _events(svc, "profile.cooldown_cleared")[0]["was_cooling"] is True
    for bad in (0, -1, 86401, True, float("nan"), "5"):
        with pytest.raises(AgentRunError, match="seconds"):
            svc.set_cooldown("reader", bad)
    for pid in ("default", "nope", None, 5):
        with pytest.raises(AgentRunError, match="unknown profile"):
            svc.set_cooldown(pid, 10)
        with pytest.raises(AgentRunError, match="unknown profile"):
            svc.clear_cooldown(pid)
    assert len(_events(svc, "profile.cooldown_set")) == 2  # refusals are not recorded as changes


def test_unaudited_cooldown_never_takes_effect_and_clear_needs_its_audit(env):
    svc = make_service(env, True, "s")
    svc.db.execute("DROP TABLE outbox")  # make the audit insert fail
    with pytest.raises(AgentRunError) as err:
        svc.set_cooldown("reader", 60)
    assert err.value.status == 503 and svc.runtime.cooldowns.remaining("reader") == 0.0
    svc.runtime.cooldowns.set("reader", 60)
    with pytest.raises(AgentRunError):
        svc.clear_cooldown("reader")
    assert svc.runtime.cooldowns.remaining("reader") > 0  # still in force: clearing needs its audit


def test_profiles_and_cooldown_end_to_end_through_a_real_daemon(env):
    import signal
    import time
    tmp, state, _ = env
    agents = write(tmp, [entry()])
    daemon = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state),
                               "--migrations", str(ROOT / "migrations"), "--claude-cli-config", str(tmp / "base.json"),
                               "--agent-config", str(agents), "--allow-same-user-agents"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")})

    def ctl(*args):
        return subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(state), *args],
                              capture_output=True, text=True, timeout=10, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    try:
        deadline = time.monotonic() + 10
        while not (state / "agentd.sock").exists() and time.monotonic() < deadline:
            assert daemon.poll() is None, daemon.stderr.read()
            time.sleep(0.02)
        listed = json.loads(ctl("profiles").stdout)["profiles"]
        assert {p["id"] for p in listed} == {"default", "reader"} and "executable" not in repr(listed)
        assert json.loads(ctl("profile-cooldown", "reader", "--seconds", "120").stdout)["cooldown_remaining_seconds"] > 119
        reader = {p["id"]: p for p in json.loads(ctl("profiles").stdout)["profiles"]}["reader"]
        assert reader["cooldown_remaining_seconds"] > 100
        bad = ctl("profile-cooldown", "default", "--seconds", "5")
        assert bad.returncode == 1 and "422" in bad.stderr
        assert json.loads(ctl("profile-cooldown", "reader", "--clear").stdout)["cooldown_remaining_seconds"] == 0.0
        reader = {p["id"]: p for p in json.loads(ctl("profiles").stdout)["profiles"]}["reader"]
        assert reader["cooldown_remaining_seconds"] == 0.0
        assert (state / "operator.token").read_text().strip() not in ctl("profiles").stdout
    finally:
        daemon.send_signal(signal.SIGTERM)
        daemon.communicate(timeout=10)
    import sqlite3
    db = sqlite3.connect(str(state / "synapse.sqlite"))
    kinds = [r[0] for r in db.execute("SELECT type FROM events WHERE type LIKE 'profile.cooldown_%' ORDER BY sequence")]
    assert kinds == ["profile.cooldown_set", "profile.cooldown_cleared"]  # the refused 'default' change left no event
