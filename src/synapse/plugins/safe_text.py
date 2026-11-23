"""Bounded, escaped text for error messages and audit rows that may carry plugin-chosen strings.

Plugin-controlled text (class names, file names, role ids, manifest values) must never reach a log,
an error or an audit row raw: it is capped first and control characters, newlines, ANSI escapes and
non-ASCII are escaped. Anything that is not an exact ``str`` is replaced, so a ``str`` subclass with a
hostile ``__repr__`` or ``__getitem__`` is never called.
"""
from __future__ import annotations

DEFAULT_LIMIT = 120
_TRUNCATED = "...[truncated]"


def bounded(value: object, limit: int = DEFAULT_LIMIT) -> str:
    if type(value) is not str:
        return "<non-text>"
    escaped = ascii(value[:limit])[1:-1]  # exact str: str.__repr__, never plugin code
    truncated = len(value) > limit or len(escaped) > 4 * limit
    return escaped[: 4 * limit] + _TRUNCATED if truncated else escaped
