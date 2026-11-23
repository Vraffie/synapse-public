"""Handoff checkpoint builder and trigger (M5.6 slice 1/2, ADR 017). Pure: no I/O, no storage, no LLM.

Not wired into any engine. It holds only ids, hashes, file ranges and short validated labels, so
no untrusted free text can become an instruction. Rules (see docs/operations/handoff-checkpoint.md):

- Hard requirements are never dropped, truncated or altered. If they do not fit the byte bound the
  result is ``CheckpointRejected("hard_requirements_exceed_bound")`` and the caller must not compact.
- Every kept item names at least one citation that is present in the checkpoint.
- Unresolved issues, then decisions, are added in id order while they fit; the rest are reported
  as an explicit omitted count and digest, never silently.
- Output is canonical JSON (sorted keys, compact, ASCII) and its SHA-256; input order is irrelevant.
- Invalid input is rejected as a whole with a typed code; nothing is cleaned up or guessed.
- ``should_checkpoint`` never guesses an unknown ``context_limit``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any

SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 16384
MAX_ITEMS_PER_CATEGORY = 64
MAX_CITATIONS = 256
MAX_CITATION_REFS_PER_ITEM = 8
MAX_LABEL_CHARS = 160
MAX_ID_CHARS = 64
MAX_PATH_CHARS = 256
MAX_LINE = 10_000_000

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CATEGORIES = ("hard_requirements", "unresolved_issues", "decisions")
_ITEM_KEYS = frozenset({"id", "label", "citations"})
_CITE_KEYS = {
    "artifact": frozenset({"id", "kind", "artifact_id", "sha256"}),
    "file_range": frozenset({"id", "kind", "path", "sha256", "start_line", "end_line"}),
}


@dataclass(frozen=True)
class CheckpointPolicy:
    max_bytes: int = MAX_CHECKPOINT_BYTES


@dataclass(frozen=True)
class CheckpointBuilt:
    checkpoint: dict[str, Any]
    canonical_json: str
    sha256: str
    complete: bool  # False when optional items were omitted (counted inside the checkpoint)


@dataclass(frozen=True)
class CheckpointRejected:
    code: str  # machine code only; never echoes input text
    field: str = ""


class _Reject(Exception):
    def __init__(self, code: str, field: str = "") -> None:
        self.code, self.field = code, field


def _is_id(v: object) -> bool:
    return type(v) is str and 0 < len(v) <= MAX_ID_CHARS and _ID.match(v) is not None


def _clean_text(v: object, limit: int) -> bool:
    if type(v) is not str or not v.strip() or len(v) > limit or v != v.strip():
        return False
    if "<<<" in v or ">>>" in v:
        return False
    return all(unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in v)


def _int(v: object) -> bool:
    return type(v) is int and 1 <= v <= MAX_LINE


def _check_citation(c: object) -> dict[str, Any]:
    if type(c) is not dict or type(c.get("kind")) is not str or c["kind"] not in _CITE_KEYS:
        raise _Reject("invalid_input", "citations")
    if set(c) != _CITE_KEYS[c["kind"]] or not _is_id(c["id"]):
        raise _Reject("invalid_input", "citations")
    if type(c["sha256"]) is not str or not _SHA.match(c["sha256"]):
        raise _Reject("invalid_input", "citations.sha256")
    if c["kind"] == "artifact":
        if not _is_id(c["artifact_id"]):
            raise _Reject("invalid_input", "citations.artifact_id")
    else:
        p = c["path"]
        if (not _clean_text(p, MAX_PATH_CHARS) or p.startswith("/") or "\\" in p
                or ".." in p.split("/") or "" in p.split("/")):
            raise _Reject("invalid_input", "citations.path")
        if not (_int(c["start_line"]) and _int(c["end_line"]) and c["start_line"] <= c["end_line"]):
            raise _Reject("invalid_input", "citations.lines")
    return dict(c)


def _check_items(name: str, items: object, cites: dict[str, dict]) -> list[dict[str, Any]]:
    if type(items) not in (list, tuple):
        raise _Reject("invalid_input", name)
    if len(items) > MAX_ITEMS_PER_CATEGORY:
        raise _Reject("too_many_items", name)
    out: dict[str, dict] = {}
    for it in items:
        if type(it) is not dict or set(it) != _ITEM_KEYS:
            raise _Reject("invalid_input", name)
        if not _is_id(it["id"]) or it["id"] in out:
            raise _Reject("invalid_input", name + ".id")
        if not _clean_text(it["label"], MAX_LABEL_CHARS):
            raise _Reject("invalid_input", name + ".label")
        refs = it["citations"]
        if type(refs) not in (list, tuple) or not 0 < len(refs) <= MAX_CITATION_REFS_PER_ITEM:
            raise _Reject("missing_citation", name)
        if not all(_is_id(r) and r in cites for r in refs) or len(set(refs)) != len(refs):
            raise _Reject("invalid_citation_ref", name)
        out[it["id"]] = {"id": it["id"], "label": it["label"], "citations": sorted(refs)}
    return [out[k] for k in sorted(out)]


def _render(kept: dict[str, list[dict]], omitted: list[str], cites: dict[str, dict]) -> tuple[dict, str]:
    used = sorted({r for cat in _CATEGORIES for it in kept[cat] for r in it["citations"]})
    doc = {
        "schema_version": SCHEMA_VERSION,
        "kind": "handoff_checkpoint",
        "labels_are_data": True,
        "hard_requirements": kept["hard_requirements"],
        "unresolved_issues": kept["unresolved_issues"],
        "decisions": kept["decisions"],
        "citations": [cites[i] for i in used],
        "omitted_count": len(omitted),
        "omitted_digest": hashlib.sha256("\n".join(sorted(omitted)).encode("ascii")).hexdigest(),
    }
    return doc, json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def build_checkpoint(
    *,
    hard_requirements: object,
    unresolved_issues: object,
    decisions: object,
    citations: object,
    policy: CheckpointPolicy | None = None,
) -> CheckpointBuilt | CheckpointRejected:
    """Items are ``{"id", "label", "citations": [citation ids]}``; citations are
    ``{"id","kind":"artifact","artifact_id","sha256"}`` or
    ``{"id","kind":"file_range","path","sha256","start_line","end_line"}``. Never raises."""
    try:
        policy = policy or CheckpointPolicy()
        if type(policy) is not CheckpointPolicy or type(policy.max_bytes) is not int or not 256 <= policy.max_bytes <= MAX_CHECKPOINT_BYTES:
            return CheckpointRejected("invalid_policy", "max_bytes")
        if type(citations) not in (list, tuple):
            raise _Reject("invalid_input", "citations")
        if len(citations) > MAX_CITATIONS:
            raise _Reject("too_many_items", "citations")
        cites: dict[str, dict] = {}
        for c in citations:
            checked = _check_citation(c)
            if checked["id"] in cites:
                raise _Reject("invalid_input", "citations.id")
            cites[checked["id"]] = checked
        raw = {"hard_requirements": hard_requirements, "unresolved_issues": unresolved_issues, "decisions": decisions}
        items = {n: _check_items(n, raw[n], cites) for n in _CATEGORIES}
        ids = [i["id"] for n in _CATEGORIES for i in items[n]]
        if len(set(ids)) != len(ids):
            raise _Reject("invalid_input", "id")

        kept: dict[str, list[dict]] = {"hard_requirements": items["hard_requirements"], "unresolved_issues": [], "decisions": []}
        optional = [(n, i) for n in ("unresolved_issues", "decisions") for i in items[n]]
        omitted = [i["id"] for _, i in optional]
        doc, text = _render(kept, omitted, cites)
        if len(text.encode("ascii")) > policy.max_bytes:
            return CheckpointRejected("hard_requirements_exceed_bound")
        for pos, (name, item) in enumerate(optional):
            trial = {k: list(v) for k, v in kept.items()}
            trial[name].append(item)
            t_omitted = [i["id"] for _, i in optional[pos + 1:]]
            t_doc, t_text = _render(trial, t_omitted, cites)
            if len(t_text.encode("ascii")) > policy.max_bytes:
                break  # prefix rule: stop at the first optional item that does not fit
            kept, omitted, doc, text = trial, t_omitted, t_doc, t_text
        return CheckpointBuilt(doc, text, hashlib.sha256(text.encode("ascii")).hexdigest(), not omitted)
    except _Reject as r:
        return CheckpointRejected(r.code, r.field)
    except Exception:  # hostile input must never raise out of a pure builder
        return CheckpointRejected("malformed_input")


# ------------------------------------------------------------------------------------------ trigger
@dataclass(frozen=True)
class TriggerPolicy:
    recommend_bp: int = 7500  # used/limit in basis points at or above which a checkpoint is recommended
    require_bp: int = 9000  # at or above which it is required


@dataclass(frozen=True)
class TriggerDecision:
    action: str  # none | recommended | required | unknown_limit | unknown_usage | invalid
    reason: str
    used_tokens: int | None = None
    context_limit: int | None = None
    ratio_bp: int | None = None


def _count(v: object) -> bool:
    return type(v) is int and 0 <= v <= 10**12


def should_checkpoint(usage: object, context_limit: object, policy: TriggerPolicy | None = None) -> TriggerDecision:
    """``usage`` is ``(input_tokens, output_tokens)`` with ``None`` for unknown (the shape of
    ``UsageObservation``). ``context_limit=None`` means unknown and is never guessed. Unknown output
    tokens make usage a lower bound: it can still trigger, but cannot prove "none"."""
    try:
        policy = policy or TriggerPolicy()
        if (type(policy) is not TriggerPolicy or type(policy.recommend_bp) is not int
                or type(policy.require_bp) is not int or not 0 < policy.recommend_bp <= policy.require_bp <= 10000):
            return TriggerDecision("invalid", "invalid_policy")
        if type(usage) not in (tuple, list) or len(usage) != 2:
            return TriggerDecision("invalid", "invalid_usage")
        inp, out = usage
        if (inp is not None and not _count(inp)) or (out is not None and not _count(out)):
            return TriggerDecision("invalid", "invalid_usage")
        if context_limit is None:
            return TriggerDecision("unknown_limit", "context_limit unknown; no limit is guessed", None if inp is None else inp + (out or 0))
        if type(context_limit) is not int or not 0 < context_limit <= 10**12:
            return TriggerDecision("invalid", "invalid_context_limit")
        if inp is None:
            return TriggerDecision("unknown_usage", "input_tokens unknown", None, context_limit)
        used = inp + (out or 0)
        ratio = used * 10000 // context_limit
        if used * 10000 >= context_limit * policy.require_bp:
            return TriggerDecision("required", "at_or_above_require_threshold", used, context_limit, ratio)
        if used * 10000 >= context_limit * policy.recommend_bp:
            return TriggerDecision("recommended", "at_or_above_recommend_threshold", used, context_limit, ratio)
        if out is None:
            return TriggerDecision("unknown_usage", "output_tokens unknown; usage is a lower bound", used, context_limit, ratio)
        return TriggerDecision("none", "below_thresholds", used, context_limit, ratio)
    except Exception:
        return TriggerDecision("invalid", "malformed_input")
