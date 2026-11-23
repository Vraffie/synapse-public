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
    (entry(provider="codex"), "disabled until W3"),
    (entry(provider="gemini"), "provider must be 'claude'"),
    (entry(executable="/bin/sh"), "unknown key"),
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


def test_daemon_requires_base_config_and_rejects_codex(env):
    tmp, state, _ = env
    r = run_daemon(state, "--agent-config", str(write(tmp, [entry()])))
    assert r.returncode != 0 and "--claude-cli-config" in r.stderr
    codex = write(tmp, [entry(provider="codex")])
    r = run_daemon(state, "--claude-cli-config", str(tmp / "base.json"), "--agent-config", str(codex),
                   "--allow-same-user-agents")
    assert r.returncode != 0 and "disabled until W3" in r.stderr
