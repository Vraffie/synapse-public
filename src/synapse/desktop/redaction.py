"""Display-time redaction of event JSON. Anything keyed like a credential is never rendered."""
from __future__ import annotations

import json
import re

REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(r"token|secret|credential|passw(or)?d|passwd|api[_-]?key|authorization|private[_-]?key", re.IGNORECASE)
MAX_DEPTH = 12
MAX_TEXT = 20000


def redact(value, _depth: int = 0):
    """Return a copy of a JSON value with sensitive-keyed fields replaced; deep or odd values are bounded."""
    if _depth > MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {str(key): REDACTED if SENSITIVE_KEY.search(str(key)) else redact(item, _depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value[:500]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4096]


def redacted_json(value) -> str:
    text = json.dumps(redact(value), indent=2, sort_keys=True, ensure_ascii=True)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n… [truncated]"
