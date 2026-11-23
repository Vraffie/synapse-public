"""Strict operator config: repos allowlist and worktree_root (see docs/operations/agent-runs.md)."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from synapse.daemon.agent_config import AgentConfigError, load_agent_config, load_claude_cli_profile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    base = {"executable": str(exe), "allowed_tools": ["Read"], "enabled": True,
            "repos": [str(repo.resolve())], "worktree_root": str((tmp_path / "wt").resolve())}

    def write(**over):
        data = {**base, **over}
        data = {k: v for k, v in data.items() if v is not ...}
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path
    return state, base, write, tmp_path


def test_valid_config_with_repos(world):
    state, base, write, _ = world
    cfg = load_agent_config(write(), state)
    assert cfg.repos == tuple(base["repos"]) and cfg.worktree_root == base["worktree_root"] and cfg.profile.enabled
    assert load_claude_cli_profile(write(), state).allowed_tools == ("Read",)  # legacy profile-only loader


def test_config_without_repos_is_still_valid_and_has_none(world):
    state, _, write, _ = world
    cfg = load_agent_config(write(repos=..., worktree_root=...), state)
    assert cfg.repos == () and cfg.worktree_root is None


def bad_cases(tmp_path, state):
    repo = str((tmp_path / "repo").resolve())
    return {
        "repos without root": {"worktree_root": ...},
        "root without repos": {"repos": ...},
        "repos not a list": {"repos": repo},
        "repos empty": {"repos": []},
        "repos too many": {"repos": [repo + "%d" % i for i in range(33)]},
        "repo relative": {"repos": ["repo"]},
        "repo nonstring": {"repos": [1]},
        "repo duplicate": {"repos": [repo, repo]},
        "repo not normalized": {"repos": [repo + "/../repo"]},
        "repo not git": {"repos": [str(tmp_path)]},
        "repo is state dir": {"repos": [str(state)]},
        "repo contains state dir": {"repos": [str(tmp_path)]},
        "root relative": {"worktree_root": "wt"},
        "root in state dir": {"worktree_root": str(state / "wt")},
        "root is state dir": {"worktree_root": str(state)},
        "root contains state dir": {"worktree_root": str(tmp_path)},
        "root inside repo": {"worktree_root": repo + "/wt"},
        "root is repo": {"worktree_root": repo},
        "root nonstring": {"worktree_root": 5},
        "unknown key": {"bypass": True},
    }


@pytest.mark.parametrize("name", sorted(bad_cases(Path("/x"), Path("/s"))))
def test_invalid_repos_config_is_refused(world, name):
    state, _, write, tmp_path = world
    over = bad_cases(tmp_path, state)[name]
    with pytest.raises(AgentConfigError):
        load_agent_config(write(**over), state)


def test_symlinked_repo_is_refused(world):
    state, _, write, tmp_path = world
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "repo")
    with pytest.raises(AgentConfigError):
        load_agent_config(write(repos=[str(link)]), state)


def test_daemon_refuses_to_start_with_bad_repos_before_touching_state(world):
    state, _, write, tmp_path = world
    path = write(repos=["relative"])
    done = subprocess.run([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state), "--migrations", str(ROOT / "migrations"),
                           "--claude-cli-config", str(path)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert done.returncode != 0 and "AgentConfigError" in done.stderr
    assert not (state / "synapse.sqlite").exists() and not (tmp_path / "wt").exists()


@pytest.mark.parametrize("tool", ["default", "Default", "", " ", "default(x)"])
def test_reserved_tool_name_in_profile_is_refused(world, tool):
    state, _, write, _ = world
    with pytest.raises(AgentConfigError):
        load_agent_config(write(allowed_tools=["Read", tool]), state)


def test_task_spec_cannot_request_reserved_tool(tmp_path):
    from synapse.persistence import agent_tasks as at
    spec = {"prompt": "x", "worktree": str(tmp_path), "allowed_tools": ["default"],
            "permission_mode": "plan", "timeout_seconds": 10}
    with pytest.raises(at.AgentSpecError):
        at.parse_task_specs({"n1": spec}, operator_tools=("Read",), max_timeout=60, state_dir=str(tmp_path))
