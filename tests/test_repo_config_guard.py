import logging
import os

import pytest

import synapse.providers.repo_config_guard as g
from synapse.providers.repo_config_guard import check_repo_config, scan_repo_config


def touch(root, rel, content="x"):
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def test_clean_tree_passes(tmp_path):
    touch(tmp_path, "src/a.py")
    for ad in ("claude", "codex"):
        check_repo_config(str(tmp_path), ad)


@pytest.mark.parametrize("rel,claude,codex", [
    (".claude/settings.json", True, False),
    ("CLAUDE.md", True, False),
    ("AGENTS.md", False, True),
    ("AGENTS.override.md", False, True),
    (".codex/config.toml", False, True),
    (".agents/skills/x/SKILL.md", False, True),
    (".mcp.json", True, True),
    (".cursorrules", False, False),
    (".cursor/rules/a.mdc", False, False),
    (".github/copilot-instructions.md", False, False),
    (".windsurfrules", False, False),
    (".aider.conf.yml", False, False),
    (".continue/config.json", False, False),
    (".gemini/settings.json", False, False),
    (".clinerules", False, False),
])
def test_matrix(tmp_path, rel, claude, codex, caplog):
    touch(tmp_path, rel)
    for ad, hard in (("claude", claude), ("codex", codex)):
        if hard:
            with pytest.raises(ValueError, match="refused"):
                check_repo_config(str(tmp_path), ad)
        else:
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                check_repo_config(str(tmp_path), ad)
            assert "worktree contains" in caplog.text
    check_repo_config(str(tmp_path), "claude", allow=True)
    check_repo_config(str(tmp_path), "codex", allow=True)


def test_nested_dirs(tmp_path):
    touch(tmp_path, "pkg/sub/AGENTS.md")
    touch(tmp_path, "pkg/CLAUDE.md")
    with pytest.raises(ValueError, match="AGENTS.md"):
        check_repo_config(str(tmp_path), "codex")
    with pytest.raises(ValueError, match="CLAUDE.md"):
        check_repo_config(str(tmp_path), "claude")


def test_nested_root_only_entries_ignored(tmp_path):
    touch(tmp_path, "pkg/.mcp.json")
    touch(tmp_path, "pkg/.github/copilot-instructions.md")
    assert scan_repo_config(str(tmp_path), "claude") == []


@pytest.mark.parametrize("name", ["agents.md", "Agents.MD", ".CODEX/x", ".Agents/x"])
def test_case_variants(tmp_path, name):
    touch(tmp_path, name)
    with pytest.raises(ValueError):
        check_repo_config(str(tmp_path), "codex")


def test_symlinked_config_file_and_dangling(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    os.symlink("real.txt", tmp_path / "AGENTS.md")
    os.symlink("/nonexistent/target", tmp_path / "CLAUDE.md")
    with pytest.raises(ValueError):
        check_repo_config(str(tmp_path), "codex")
    with pytest.raises(ValueError):
        check_repo_config(str(tmp_path), "claude")


def test_symlinked_dir_hit_but_not_followed(tmp_path):
    outside = tmp_path / "outside"
    touch(outside, "AGENTS.md")
    wt = tmp_path / "wt"
    wt.mkdir()
    os.symlink(outside, wt / "link")
    os.symlink(outside, wt / ".codex")
    assert [h.path for h in scan_repo_config(str(wt), "codex")] == [".codex"]


def test_dot_git_contents_ignored(tmp_path):
    touch(tmp_path, ".git/AGENTS.md")
    touch(tmp_path, ".git/hooks/CLAUDE.md")
    touch(tmp_path, ".git/.mcp.json")
    touch(tmp_path, ".GIT/AGENTS.md")
    check_repo_config(str(tmp_path), "codex")
    check_repo_config(str(tmp_path), "claude")


def test_dot_git_file_ignored(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /x")
    check_repo_config(str(tmp_path), "codex")


def test_scan_cap_fails_closed(tmp_path, monkeypatch):
    for i in range(5):
        touch(tmp_path, f"f{i}")
    monkeypatch.setattr(g, "MAX_ENTRIES", 3)
    with pytest.raises(ValueError, match="too large"):
        check_repo_config(str(tmp_path), "claude")


def test_unknown_adapter(tmp_path):
    with pytest.raises(ValueError):
        scan_repo_config(str(tmp_path), "other")


def test_depth_beyond_max_fails_closed(tmp_path):
    rel = "/".join(["d"] * (g.MAX_DEPTH + 2)) + "/AGENTS.md"
    touch(tmp_path, rel)
    with pytest.raises(ValueError, match="deeper"):
        check_repo_config(str(tmp_path), "codex")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_unreadable_subdirectory_fails_closed(tmp_path):
    touch(tmp_path, "sub/x")
    (tmp_path / "sub").chmod(0)
    try:
        with pytest.raises(ValueError, match="unreadable"):
            check_repo_config(str(tmp_path), "claude")
    finally:
        (tmp_path / "sub").chmod(0o700)
