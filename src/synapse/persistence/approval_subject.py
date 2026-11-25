"""Human-readable, bounded approval subject for agent-run approvals (security review F6).

The approver is shown WHAT they are approving, not an opaque digest: repo, base ref, tool set,
permission mode, timeout, a bounded preview of the prompt (read from its private, digest-verified
artifact) and the full-spec hash the approval is bound to. Read-only; derived only from the run's
single trusted ``agent.tasks_configured`` record, never from client input. It contains no tokens.
"""
from __future__ import annotations

from collections.abc import Callable
import re
import sqlite3
from typing import Any

from synapse.persistence import agent_tasks as at

SUBJECT_PROMPT_BYTES = 8 * 1024  # preview bound (the stored prompt itself is at most 32 KiB)
MAX_SUBJECT_TOOLS = at.MAX_ALLOWED_TOOLS
# C0/C1 controls (newline and tab are kept), DEL, bidi marks/embeddings/overrides/isolates, line/paragraph separators.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def sanitize_preview(text: str) -> str:
    """Make the preview safe to render for an approver: unsafe characters become visible ``\\uXXXX`` escapes."""
    return _UNSAFE.sub(lambda m: "\\u%04x" % ord(m.group()), text)

BlobReader = Callable[[str], bytes]


def agent_subject(db: sqlite3.Connection, read_blob: BlobReader | None, run_id: str, candidate_sha256: str, *,
                  with_prompt: bool = True) -> dict[str, Any] | None:
    """The subject for the approval bound to ``candidate_sha256`` in ``run_id``, or None when the run is
    not a consistent agent run or no recorded approval step carries that hash."""
    try:
        if not at.is_agent_run(db, run_id):
            return None
        specs = at.load_task_specs(db, run_id) or {}
    except Exception:
        return None
    for node, spec in sorted(specs.items()):
        step = spec["approval_step"]
        if step is None or step["candidate_sha256"] != candidate_sha256:
            continue
        preview, truncated, unavailable = None, False, False
        if with_prompt and read_blob is not None:
            try:
                data = read_blob(spec["prompt_sha256"])
                if len(data) != spec["prompt_bytes"]:
                    raise ValueError("length mismatch")
                truncated = len(data) > SUBJECT_PROMPT_BYTES
                preview = sanitize_preview(data[:SUBJECT_PROMPT_BYTES].decode("utf-8", "ignore"))
                if len(preview.encode("utf-8")) > SUBJECT_PROMPT_BYTES:  # escapes expanded it: re-bound, say so
                    preview = preview.encode("utf-8")[:SUBJECT_PROMPT_BYTES].decode("utf-8", "ignore")
                    truncated = True
            except Exception:
                unavailable = True
        profile = {"profile": spec["profile_id"]} if spec.get("profile_id") else {}
        if "repos" in spec:  # ADR 026 D5: multi-repository subject; no path or attempt HEAD
            from synapse.persistence.repo_bindings import load_run_repos
            bound = load_run_repos(db, run_id)
            return {**profile, "kind": "agent_run_multi", "node_id": node,
                    "repos": [{"repo_id": r["repo_id"], "access": r["access"],
                               "baseline_ref": bound[r["repo_id"]].baseline_ref,
                               "baseline_sha": bound[r["repo_id"]].baseline_sha} for r in spec["repos"]],
                    "allowed_tools": spec["allowed_tools"][:MAX_SUBJECT_TOOLS],
                    "allowed_tools_truncated": len(spec["allowed_tools"]) > MAX_SUBJECT_TOOLS,
                    "permission_mode": spec["permission_mode"], "timeout_seconds": spec["timeout_seconds"],
                    "prompt_bytes": spec["prompt_bytes"], "prompt_preview": preview,
                    "prompt_preview_truncated": truncated, "prompt_unavailable": unavailable,
                    "spec_sha256": candidate_sha256}
        return {**profile, "kind": "agent_run", "node_id": node, "repo": spec["repo"], "base_ref": spec["base_ref"],
                "worktree_head": spec["worktree_head"], "allowed_tools": spec["allowed_tools"][:MAX_SUBJECT_TOOLS],
                "allowed_tools_truncated": len(spec["allowed_tools"]) > MAX_SUBJECT_TOOLS,
                "permission_mode": spec["permission_mode"], "timeout_seconds": spec["timeout_seconds"],
                "prompt_bytes": spec["prompt_bytes"], "prompt_preview": preview,
                "prompt_preview_truncated": truncated, "prompt_unavailable": unavailable,
                "spec_sha256": candidate_sha256}
    return None


def manifest_subject(db: sqlite3.Connection, read_blob: BlobReader | None, run_id: str,
                     candidate_sha256: str) -> dict[str, Any] | None:
    """The subject for an approval bound to a v2 candidate manifest of ``run_id`` (ADR 026 D9), or None when the
    hash is not a manifest of that run. Shows every repository's pinned baseline and, for write repositories, the
    candidate commit and change size, plus any invalidation. The repositories come from the stored, hash-verified
    manifest bytes (null with ``manifest_unavailable`` when they cannot be read); never paths or file names."""
    from synapse.application.candidate_manifest_v2 import manifest_v2_from_bytes
    from synapse.persistence import candidate_manifests as cm
    try:
        if not cm.tables_present(db):
            return None
        record = cm.load_manifest(db, candidate_sha256)
        if record is None or record.run_id != run_id:
            return None
        reasons = cm.invalidations(db, candidate_sha256)
    except Exception:
        return None
    repos, unavailable = None, True
    if read_blob is not None:
        try:
            manifest = manifest_v2_from_bytes(read_blob(candidate_sha256))
            if manifest.manifest_sha256 == candidate_sha256 and manifest.run_id == run_id:
                repos = [{"repo_id": r.repo_id, "access": r.access, "baseline_ref": r.baseline_ref,
                          "baseline_sha": r.baseline_sha, "candidate_sha": r.candidate_sha,
                          "changed_paths": r.changed_paths, "diff_bytes": r.diff_bytes} for r in manifest.repos]
                unavailable = False
        except Exception:
            repos, unavailable = None, True
    return {"kind": "candidate_manifest", "manifest_sha256": candidate_sha256, "seq": record.seq,
            "bindings_sha256": record.bindings_sha256, "invalidated": reasons, "repos": repos,
            "manifest_unavailable": unavailable}


def approval_subject(db: sqlite3.Connection, read_blob: BlobReader | None, run_id: str, candidate_sha256: str, *,
                     with_prompt: bool = True) -> dict[str, Any] | None:
    """A candidate-manifest subject when the approval binds a manifest of the run, else the agent-run subject."""
    found = manifest_subject(db, read_blob, run_id, candidate_sha256)
    if found is not None:
        return found
    return agent_subject(db, read_blob, run_id, candidate_sha256, with_prompt=with_prompt)
