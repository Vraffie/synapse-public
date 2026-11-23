"""Deterministic reducer for tool/command output that Synapse stores or forwards (backlog M5.3).

Pure: no I/O, no clock, no randomness, no LLM. ``reduce_output`` makes a bounded, readable version of
one output blob and returns the SHA-256 of the *raw* bytes so a caller that stored the raw output as an
artifact can cite it. The raw text itself is never stored here.

Guarantees (each is exercised by tests/test_output_reducer.py):

* every distinct failure line (see ``kept_failures``) survives, either verbatim or as the prefix of
  ``"<line> (xN)"``; failure blocks (a traceback with its frames and exception line, a pytest ``E`` block,
  compiler diagnostics with their indented context ...) are kept whole, up to ``MAX_CONT`` continuation lines;
* the output never exceeds ``max_bytes`` unless the failure blocks alone do (``over_budget=True``);
* a short head and tail window is kept while the budget allows (the last line first);
* deterministic; bounded memory (one pass, bounded windows) and linear time (no backtracking regexes);
* the output is no larger than the input except for small marker lines when ``over_budget``.

Non-guarantees: lines longer than ``MAX_LINE`` are cut (marked ``…[+N chars]``), so a failure line that
long is not verbatim; at most ``MAX_FAIL_BLOCKS`` blocks / ``MAX_FAIL_BYTES`` of failure text are kept
(then ``failure_cap`` is reported and ``truncated`` is true); repeated identical failure blocks are kept
once with a repeat note; classification is textual, so a non-failure line containing e.g. ``Error`` is
kept too (the reducer errs towards keeping).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import re

KINDS = ("generic", "pytest", "compiler", "git", "json")
MIN_MAX_BYTES = 64
HEAD_LINES = 20
TAIL_LINES = 20
MAX_LINE = 4096
MAX_CONT = 40
MAX_FAIL_BLOCKS = 1000
MAX_FAIL_BYTES = 1024 * 1024
MAX_FAIL_LIST = 200
_GROUP_BUFFER = 32
_JSON_MAX_BYTES = 8 * 1024 * 1024
_JSON_FAILURE_KEYS = frozenset({"error", "errors", "failure", "failures", "exception", "stderr", "message", "traceback"})
_JSON_LEVELS = ((50, 1000), (20, 300), (8, 120), (3, 60))

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DIGITS = re.compile(r"[0-9]+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FAIL_TOKENS = frozenset({"Traceback", "Exception", "Error", "ERROR", "FATAL", "FAILED", "FAIL", "FAILURE",
                          "PANIC", "panic", "AssertionError"})
_GENERIC_LEAD = re.compile(r"^\s{0,40}(?:error|fatal|failed|failure|panic)\b[:\s]", re.IGNORECASE)
_PY_STATUS = re.compile(r"^(?:FAILED|ERROR) \S")
_PY_HEADER = re.compile(r"^_{2,} .{1,200} _{2,}$")
_PY_SECTION = re.compile(r"^={3,} .{1,200} ={3,}$")
_PY_LOCATION = re.compile(r"^[\w./\\-]{1,300}\.py:\d{1,9}: ")
_CC_DIAG = re.compile(r"^[^\s:][^:\n]{0,300}:\d{1,9}(?::\d{1,9})?: (?:fatal error|error|warning)\b")
_CC_LEAD = re.compile(r"^(?:error|warning)(?:\[\w{1,20}\])?:")
_GIT_LEAD = re.compile(r"^(?:fatal|error|warning|hint|remote: error):")
_GIT_MARK = re.compile(r"^(?:<{7}|={7}|>{7})(?: |$)")
_SECTION_WORDS = frozenset({"failed", "error", "errors", "failures", "summary"})


@dataclass(frozen=True, slots=True)
class ReducedOutput:
    reduced: str
    raw_sha256: str
    raw_bytes: int
    reduced_bytes: int
    dropped_lines: int
    kept_failures: list[str] = field(default_factory=list)
    truncated: bool = False
    rules_applied: list[str] = field(default_factory=list)
    over_budget: bool = False


# ------------------------------------------------------------------ classification
def _generic_failure(line: str) -> bool:
    for word in _WORD.findall(line):
        if word in _FAIL_TOKENS or (len(word) > 5 and (word.endswith("Error") or word.endswith("Exception"))):
            return True
    return False


def _classify(line: str, kind: str) -> int:
    """0 = not a failure line, 1 = failure line, 2 = start of a Python traceback."""
    stripped = line.lstrip()
    if stripped.startswith("Traceback (most recent call last):"):
        return 2
    if stripped.startswith(("During handling of the above exception", "The above exception was the direct cause")):
        return 1
    if kind == "pytest":
        if (_PY_STATUS.match(line) or _PY_HEADER.match(line) or _PY_LOCATION.match(line)
                or line.startswith("E   ") or line == "E"):
            return 1
        if _PY_SECTION.match(line) and _SECTION_WORDS & {w.lower() for w in _WORD.findall(line)}:
            return 1
    elif kind == "compiler":
        if (_CC_DIAG.match(line) or _CC_LEAD.match(line) or line.startswith(("make: ***", "collect2:", "ld: "))
                or "undefined reference" in line or stripped.startswith("--> ")):
            return 1
    elif kind == "git":
        if _GIT_LEAD.match(line) or line.startswith("CONFLICT") or _GIT_MARK.match(line):
            return 1
    return 1 if (_GENERIC_LEAD.match(line) or _generic_failure(line)) else 0


def _blen(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8", "replace"))


def _is_marker(text: str) -> bool:
    return text.startswith("[... ") and text.endswith(" similar lines]")


# ------------------------------------------------------------------ line normalisation
def _normalise(line: str, rules: dict) -> str:
    if "\r" in line:
        parts = [p for p in line.split("\r") if p.strip()]
        line = parts[-1] if parts else ""
        rules["carriage_return"] = None
    if "\x1b" in line:
        cleaned = _ANSI.sub("", line)
        if cleaned != line:
            rules["ansi"] = None
        line = cleaned
    if _CTRL.search(line) or "\x1b" in line:
        line = _CTRL.sub("", line).replace("\x1b", "")
        rules["control_chars"] = None
    line = line.rstrip(" \t")
    if len(line) > MAX_LINE:
        line = f"{line[:MAX_LINE]} …[+{len(line) - MAX_LINE} chars]"
        rules["long_line"] = None
    return line


def _iter_lines(text: str):
    start, size = 0, len(text)
    while start < size:
        end = text.find("\n", start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


# ------------------------------------------------------------------ collapsing
class _Group:
    __slots__ = ("key", "failure", "first", "second", "last", "count", "identical", "lines", "bytes")

    def __init__(self, key: str, failure: bool, line: str) -> None:
        self.key, self.failure, self.first, self.second, self.last = key, failure, line, None, line
        self.count, self.identical = 1, True
        self.lines: list[str] | None = [line]
        self.bytes = _blen(line) + 1

    def add(self, line: str) -> None:
        self.count += 1
        if line != self.first:
            self.identical = False
        if self.count == 2:
            self.second = line
        self.last = line
        self.bytes += _blen(line) + 1
        if self.lines is not None:
            if self.count > _GROUP_BUFFER:
                self.lines = None
            else:
                self.lines.append(line)

    def emit(self, rules: dict) -> list[str]:
        if self.count == 1:
            return [self.first]
        if self.identical:
            if self.first:
                collapsed, rule = [f"{self.first} (x{self.count})"], "collapse_repeats"
            else:
                collapsed, rule = [""], "blank_runs"
        elif self.count == 2:
            return [self.first, self.second]
        else:
            collapsed, rule = [self.first, f"[... {self.count - 2} similar lines]", self.last], "collapse_similar"
        if self.lines is not None and sum(_blen(t) + 1 for t in collapsed) >= self.bytes:
            return list(self.lines)  # collapsing would not make it smaller
        rules[rule] = None
        return collapsed


class _Reducer:
    def __init__(self, kind: str, max_bytes: int) -> None:
        self.kind, self.max_bytes = kind, max_bytes
        self.rules: dict[str, None] = {}
        self.total_in = 0
        self.units = 0
        self.pending_blank = 0
        self.group: _Group | None = None
        self.all_lines: list[str] | None = []
        self.all_bytes = 0
        self.head: list[tuple[int, str]] = []
        self.tail: deque[tuple[int, str]] = deque(maxlen=TAIL_LINES)
        self.blocks: list[_Block] = []
        self.block_keys: dict[str, _Block] = {}
        self.fail_bytes = 0
        self.cap_hit = False
        self.cur: list[tuple[int, str]] | None = None
        self.cont = 0
        self.in_tb = False
        self.failures: list[str] = []
        self.failure_set: set[str] = set()

    # --- input side
    def push(self, raw_line: str) -> None:
        self.total_in += 1
        line = _normalise(raw_line, self.rules)
        if not line:
            self.pending_blank += 1  # trailing blank lines carry nothing: only emitted if text follows
            return
        while self.pending_blank:
            self.pending_blank -= 1
            self._feed("")
        self._feed(line)

    def _feed(self, line: str) -> None:
        failure = _classify(line, self.kind) > 0
        key = line if failure else _DIGITS.sub("#", line)
        group = self.group
        if group is not None and group.key == key and group.failure == failure:
            group.add(line)
            return
        if group is not None:
            self._emit_group(group)
        self.group = _Group(key, failure, line)

    def finish(self) -> None:
        if self.group is not None:
            self._emit_group(self.group)
            self.group = None
        self._close_block()

    def _emit_group(self, group: _Group) -> None:
        for text in group.emit(self.rules):
            self._unit(text)

    # --- one emitted unit
    def _unit(self, text: str) -> None:
        idx = self.units
        self.units += 1
        if idx < HEAD_LINES:
            self.head.append((idx, text))
        self.tail.append((idx, text))
        if self.all_lines is not None:
            self.all_bytes += _blen(text) + 1
            if self.all_bytes - 1 > self.max_bytes:
                self.all_lines = None
            else:
                self.all_lines.append(text)
        if _is_marker(text):
            self._close_block()
            return
        cls = _classify(text, self.kind)
        if cls:
            if self.cur is None:
                self.cur = []
            self.cur.append((idx, text))
            self.cont, self.in_tb = MAX_CONT, cls == 2
            if text not in self.failure_set and len(self.failures) < MAX_FAIL_LIST:
                self.failure_set.add(text)
                self.failures.append(text)
        elif self.cur is not None and self.cont > 0:
            if text[:1] in (" ", "\t") or (self.kind == "pytest" and text.startswith(">")):
                self.cur.append((idx, text))
                self.cont -= 1
            elif self.in_tb and text.strip():
                self.cur.append((idx, text))
                self._close_block()
            else:
                self._close_block()
        else:
            self._close_block()

    def _close_block(self) -> None:
        lines, self.cur, self.cont, self.in_tb = self.cur, None, 0, False
        if not lines:
            return
        digest = hashlib.sha1("\n".join(t for _, t in lines).encode("utf-8", "replace")).hexdigest()
        known = self.block_keys.get(digest)
        if known is not None:
            known.repeats += 1
            self.rules["dedupe_failure_blocks"] = None
            return
        size = sum(_blen(t) + 1 for _, t in lines)
        if len(self.blocks) >= MAX_FAIL_BLOCKS or self.fail_bytes + size > MAX_FAIL_BYTES:
            self.cap_hit = True
            self.rules["failure_cap"] = None
            return
        block = _Block(lines)
        self.blocks.append(block)
        self.block_keys[digest] = block
        self.fail_bytes += size

    # --- output side
    def result(self) -> tuple[list[str], bool, bool]:
        """(output lines, truncated, over_budget)."""
        if self.units == 0:
            return [], False, False
        if self.all_lines is not None:
            return self.all_lines, False, False
        return self._assemble()

    def _assemble(self) -> tuple[list[str], bool, bool]:
        mandatory: dict[int, str] = {}
        notes: dict[int, str] = {}
        for block in self.blocks:
            for idx, text in block.lines:
                mandatory[idx] = text
            if block.repeats:
                notes[block.lines[-1][0]] = f"[... failure above repeated {block.repeats}x ...]"
        optional: list[tuple[int, str]] = []
        seen: set[int] = set()
        for idx, text in [*reversed(self.tail), *self.head]:
            if idx not in mandatory and idx not in seen:
                seen.add(idx)
                optional.append((idx, text))
        fixed = sum(_blen(t) + 1 for t in mandatory.values()) + sum(_blen(t) + 1 for t in notes.values())
        chosen: list[tuple[int, str]] = []
        used = fixed
        for idx, text in optional:
            cost = _blen(text) + 1
            if used + cost > self.max_bytes:
                break
            chosen.append((idx, text))
            used += cost
        while True:
            lines, gaps = self._build(mandatory, notes, chosen)
            size = sum(_blen(t) + 1 for t in lines) - 1
            if size <= self.max_bytes or not chosen:
                break
            chosen.pop()
        if gaps:
            self.rules["window"] = None
        return lines, gaps > 0, size > self.max_bytes

    def _build(self, mandatory, notes, chosen) -> tuple[list[str], int]:
        texts = dict(mandatory)
        texts.update(chosen)
        lines: list[str] = []
        gaps, prev = 0, -1
        for idx in sorted(texts):
            if idx - prev > 1:
                lines.append(f"[... {idx - prev - 1} lines omitted ...]")
                gaps += 1
            lines.append(texts[idx])
            if idx in notes:
                lines.append(notes[idx])
            prev = idx
        if prev < self.units - 1:
            lines.append(f"[... {self.units - 1 - prev} lines omitted ...]")
            gaps += 1
        return lines, gaps


class _Block:
    __slots__ = ("lines", "repeats")

    def __init__(self, lines: list[tuple[int, str]]) -> None:
        self.lines, self.repeats = lines, 0


# ------------------------------------------------------------------ binary and json
def _looks_binary(raw: bytes) -> bool:
    sample = raw[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    odd = sum(1 for b in sample if (b < 9 or 14 <= b < 32) and b != 27)
    return odd * 10 > len(sample)


def _prune(value, max_items: int, max_str: int, in_failure: bool = False):
    if isinstance(value, str):
        limit = 4096 if in_failure else max_str
        return value if len(value) <= limit else f"{value[:limit]}…[+{len(value) - limit} chars]"
    if isinstance(value, list):
        if len(value) > max_items:
            keep = max(1, max_items - 1)
            return [*(_prune(v, max_items, max_str, in_failure) for v in value[:keep]),
                    f"[... {len(value) - keep - 1} items omitted ...]",
                    _prune(value[-1], max_items, max_str, in_failure)]
        return [_prune(v, max_items, max_str, in_failure) for v in value]
    if isinstance(value, dict):
        return {k: _prune(v, max_items, max_str, in_failure or (isinstance(k, str) and k.lower() in _JSON_FAILURE_KEYS))
                for k, v in value.items()}
    return value


def _json_failures(value, out: list[str], depth: int = 0) -> None:
    if len(out) >= MAX_FAIL_LIST or depth > 100:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _JSON_FAILURE_KEYS and isinstance(item, str) and item:
                out.append(item[:300])
            else:
                _json_failures(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_failures(item, out, depth + 1)


def _reduce_json(raw: bytes, max_bytes: int, sha: str) -> ReducedOutput | None:
    if len(raw) > _JSON_MAX_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        rules = ["json_compact"]
        truncated = False
        for items, chars in _JSON_LEVELS:
            if len(compact.encode("utf-8", "replace")) <= max_bytes:
                break
            compact = json.dumps(_prune(value, items, chars), ensure_ascii=False, separators=(",", ":"))
            truncated = True
            if "json_prune" not in rules:
                rules.append("json_prune")
        failures: list[str] = []
        _json_failures(value, failures)
    except (ValueError, RecursionError):
        return None
    size = len(compact.encode("utf-8", "replace"))
    return ReducedOutput(compact, sha, len(raw), size, 0, failures, truncated, rules, size > max_bytes)


# ------------------------------------------------------------------ public API
def reduce_output(data: str | bytes | bytearray, *, kind: str = "generic", max_bytes: int = 16384) -> ReducedOutput:
    """Reduce one output blob; see the module docstring for the guarantees."""
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % ", ".join(KINDS))
    if type(max_bytes) is not int or max_bytes < MIN_MAX_BYTES:
        raise ValueError("max_bytes must be an integer of at least %d" % MIN_MAX_BYTES)
    if isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise TypeError("data must be str or bytes")
    sha = hashlib.sha256(raw).hexdigest()
    if _looks_binary(raw):
        text = f"[binary output omitted: {len(raw)} bytes, sha256 {sha}]"
        return ReducedOutput(text, sha, len(raw), len(text.encode()), 0, [], True, ["binary_summary"])
    rules: dict[str, None] = {}
    if kind == "json":
        reduced = _reduce_json(raw, max_bytes, sha)
        if reduced is not None:
            return reduced
        rules["json_invalid"] = None
        kind = "generic"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        rules["invalid_utf8"] = None
    reducer = _Reducer(kind, max_bytes)
    reducer.rules.update(rules)
    for line in _iter_lines(text):
        reducer.push(line)
    reducer.finish()
    lines, truncated, over_budget = reducer.result()
    body = "\n".join(lines)
    content = sum(1 for t in lines if not _is_marker(t) and not (t.startswith("[... ") and t.endswith(" ...]")))
    return ReducedOutput(
        reduced=body, raw_sha256=sha, raw_bytes=len(raw), reduced_bytes=len(body.encode("utf-8", "replace")),
        dropped_lines=max(0, reducer.total_in - content), kept_failures=list(reducer.failures),
        truncated=truncated or reducer.cap_hit, rules_applied=list(reducer.rules), over_budget=over_budget)
