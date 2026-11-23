"""The project-level Claude Code dev subagents in .claude/agents are well formed.

These files are development aids, not Synapse runtime. PyYAML is not a dependency, so the frontmatter
is parsed with a deliberately strict mini-parser (flat ``key: value`` lines). It also refuses values that a
real YAML parser would reject or reinterpret (an unquoted ``: `` or `` #`` inside a plain scalar).
"""
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "agents"
AGENT_FILES = sorted(p for p in AGENTS_DIR.glob("*.md") if p.name != "README.md")
EXPECTED = {"security-reviewer", "migration-checker", "test-runner", "adapter-verifier", "docs-sync", "pr-conflict-planner"}
REQUIRED_KEYS = {"name", "description", "tools", "model"}
KNOWN_TOOLS = {"Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit", "Agent", "Skill"}
# Dev subagents here must stay non-editing: they review, run, or plan. (An editing agent needs its own review.)
FORBIDDEN_TOOLS = {"Edit", "Write", "NotebookEdit", "Agent"}
MODELS = {"sonnet", "opus", "haiku", "inherit"}


def parse(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: missing frontmatter"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, f"{path.name}: frontmatter is not closed"
    meta: dict[str, str] = {}
    for line in head.splitlines():
        assert line and not line[0].isspace() and ": " in line, f"{path.name}: bad frontmatter line {line!r}"
        key, value = line.split(": ", 1)
        assert key not in meta, f"{path.name}: duplicate key {key}"
        assert value.strip() == value and value, f"{path.name}: empty or padded value for {key}"
        assert ": " not in value and " #" not in value and not value.startswith(("'", '"', "[", "{", "|", ">", "&", "*", "!", "%", "@", "`")), (
            f"{path.name}: value of {key} would not be a valid plain YAML scalar")
        meta[key] = value
    return meta, body


def test_all_expected_agents_exist_and_nothing_else():
    assert {p.stem for p in AGENT_FILES} == EXPECTED
    assert (AGENTS_DIR / "README.md").is_file()


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.stem)
def test_frontmatter_is_valid(path):
    meta, body = parse(path)
    assert set(meta) == REQUIRED_KEYS, f"{path.name}: keys {sorted(meta)}"
    assert meta["name"] == path.stem
    assert meta["description"].startswith("Use "), "description must say WHEN to use the agent"
    assert 40 <= len(meta["description"]) <= 400
    assert meta["model"] in MODELS
    tools = [t.strip() for t in meta["tools"].split(",")]
    assert tools and all(tools) and len(set(tools)) == len(tools)
    assert set(tools) <= KNOWN_TOOLS, f"{path.name}: unknown tools {set(tools) - KNOWN_TOOLS}"
    assert not set(tools) & FORBIDDEN_TOOLS, f"{path.name}: dev subagents must not edit files or spawn agents"
    assert len(body.strip()) > 400, "body should hold the concrete checklist"


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.stem)
def test_agents_are_bound_by_the_repo_rules(path):
    body = path.read_text(encoding="utf-8")
    lowered = body.lower()
    # No agent may merge or push; the read-only ones say so explicitly.
    assert "never" in lowered and ("merge" in lowered or "edit" in lowered or "commit" in lowered)
    assert "gh pr merge" not in lowered.replace("`gh pr merge/review/comment`", "")


def test_real_binary_runs_need_explicit_approval_wherever_mentioned():
    for name in ("adapter-verifier", "test-runner", "security-reviewer"):
        text = (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8").lower()
        assert "subscription" in text and ("ask the user" in text or "explicit" in text or "do not enable" in text), name


def test_readme_lists_every_agent():
    readme = (AGENTS_DIR / "README.md").read_text(encoding="utf-8")
    for name in EXPECTED:
        assert f"`{name}`" in readme
    assert "not part of the Synapse runtime" in readme
