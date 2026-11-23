"""Strict pre-check of a planner agent's JSON output before it is offered as ``POST /agent-runs`` ``tasks``.

The planner's text is untrusted model output. This module only parses and bounds it; it never grants
anything. The API re-validates every task against the operator profile, and the caller passes the tool
ceiling a planned task may use (normally the operator allowlist), so a planner cannot widen tools.
"""
from __future__ import annotations

from collections.abc import Collection
import json
import re

MAX_TASKS = 6
MAX_PROMPT_BYTES = 32 * 1024
MAX_TOOLS = 50
MAX_TEXT_BYTES = 256 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KEYS = frozenset({"id", "prompt", "allowed_tools", "depends_on"})


class PlannerOutputError(ValueError):
    """The planner output is not a valid task list (message is fixed text; it never echoes the output)."""


def parse_planner_output(text: str, *, tool_ceiling: Collection[str]) -> list[dict]:
    """Return normalised tasks ``[{id, prompt, allowed_tools, depends_on}]`` or raise PlannerOutputError.

    The text must be exactly one JSON array (an empty array means "could not plan safely" and returns
    ``[]``). Duplicate object keys, unknown keys, non-finite numbers, oversized or malformed tasks,
    tools outside ``tool_ceiling``, unknown, self or duplicate dependencies and cycles are all refused.
    """
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        raise PlannerOutputError("planner output is missing or too large")

    def reject_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(pairs)

    def reject_constant(_name):
        raise ValueError("non-finite number")

    try:
        data = json.loads(text.strip(), object_pairs_hook=reject_dupes, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise PlannerOutputError("planner output is not a single valid JSON array") from None
    if not isinstance(data, list) or len(data) > MAX_TASKS:
        raise PlannerOutputError("planner output must be a JSON array of at most %d tasks" % MAX_TASKS)
    ceiling = set(tool_ceiling)
    tasks: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not {"id", "prompt", "allowed_tools"} <= set(item) or set(item) - _KEYS:
            raise PlannerOutputError("each task needs id, prompt, allowed_tools and optionally depends_on")
        tid, prompt, tools, deps = item["id"], item["prompt"], item["allowed_tools"], item.get("depends_on", [])
        if not isinstance(tid, str) or not _ID_RE.fullmatch(tid):
            raise PlannerOutputError("invalid task id")
        if (not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt
                or len(prompt.encode("utf-8", "replace")) > MAX_PROMPT_BYTES):
            raise PlannerOutputError("invalid task prompt")
        if (not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS
                or any(not isinstance(t, str) for t in tools) or not set(tools) <= ceiling):
            raise PlannerOutputError("task tools must be a non-empty subset of the permitted tools")
        if (not isinstance(deps, list) or len(deps) > MAX_TASKS or any(not isinstance(d, str) for d in deps)
                or len(set(deps)) != len(deps)):
            raise PlannerOutputError("depends_on must be a list of distinct task ids")
        tasks.append({"id": tid, "prompt": prompt, "allowed_tools": sorted(set(tools)), "depends_on": sorted(deps)})
    ids = [t["id"] for t in tasks]
    if len(set(ids)) != len(ids):
        raise PlannerOutputError("task ids must be unique")
    graph = {t["id"]: t["depends_on"] for t in tasks}
    for node, deps in graph.items():
        if node in deps or not set(deps) <= set(ids):
            raise PlannerOutputError("depends_on must name other tasks of the plan")
    done: set[str] = set()
    while len(done) < len(graph):
        ready = [n for n, d in graph.items() if n not in done and set(d) <= done]
        if not ready:
            raise PlannerOutputError("task dependencies must be acyclic")
        done.update(ready)
    return tasks
