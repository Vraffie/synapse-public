"""Opaque keyset cursors for per-run pages (run tasks, run usage observations).

A cursor is base64url (no padding) of a JSON list of strings: a scope tag followed by the keyset values of the
last row on the previous page. The cursor is NOT signed or encrypted: it is opaque only by convention. The scope
tag is a short, public SHA-256 digest of the page kind and the run ID, so a cursor issued for one run (or for the
other route) is rejected instead of being applied to a different run. Anyone can re-encode a well-formed cursor
with the right scope and any key; that is accepted and harmless, because the SQL always filters by the requested
run, so such a cursor can only choose where in THAT run's rows the page starts, never show rows of another run.
Rejected (InvalidPageCursor): malformed, non-ASCII, oversized, wrong-arity or wrong-scope cursors.

Keys are daemon-generated UUIDs and ISO timestamps, so issued cursors stay far below the 1024-character limit.
``encode_page_cursor`` refuses to issue a cursor the decoder would reject (``UnpageableKey``, which the API maps to
422 like any cursor problem) rather than hand out a dead one or fail with a 500.
"""
import base64
import binascii
import hashlib
import json

DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 500
MAX_CURSOR_LENGTH = 1024
MAX_ITEM_LENGTH = 256


class InvalidPageCursor(ValueError):
    """Malformed, non-ASCII, oversized, wrong-arity or foreign (other run or route) pagination cursor."""


class UnpageableKey(InvalidPageCursor):
    """A row's keyset value cannot be carried in a valid cursor (empty, over 256 characters, or too long once
    encoded). Unreachable with daemon-generated UUID keys; typed so it is reported as 422, never as a 500."""


def check_page_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError("Invalid page size")
    return limit


def page_scope(kind: str, run_id: str) -> str:
    return hashlib.sha256(f"{kind}\0{run_id}".encode("utf-8")).hexdigest()[:16]


def _valid_items(items) -> bool:
    return all(isinstance(item, str) and 0 < len(item) <= MAX_ITEM_LENGTH for item in items)


def encode_page_cursor(scope: str, *keys: str) -> str:
    items = [scope, *keys]
    if not _valid_items(items):
        raise UnpageableKey("Page cursor key out of range")
    raw = json.dumps(items, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    cursor = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise UnpageableKey("Page cursor would exceed the cursor size limit")
    return cursor


def decode_page_cursor(cursor: str, scope: str, keys: int) -> list[str]:
    """Return the ``keys`` keyset values; anything else, including another scope's cursor, is InvalidPageCursor."""
    try:
        if not isinstance(cursor, str) or not cursor or not cursor.isascii() or len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, list) or len(value) != keys + 1 or not _valid_items(value) or value[0] != scope:
            raise ValueError
        return value[1:]
    except (ValueError, binascii.Error):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise InvalidPageCursor("Invalid cursor") from None
