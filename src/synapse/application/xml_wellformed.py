"""Well-formedness check for one XML blob (ADR 026 D3). Pure, stdlib only (``xml.parsers.expat``).

The caller hands in blob bytes from the git object store, never a path the agent chose. Refused, before or while
parsing:

* any ``<!DOCTYPE`` / ``<!ENTITY`` or other ``<!`` declaration except comments (``<!--``) and ``<![CDATA[``, found by
  a byte pre-scan, so external entities and entity expansion are excluded by construction; expat additionally runs
  with parameter-entity parsing off and doctype/entity handlers that raise;
* more than ``MAX_XML_BYTES`` (8 MiB) or an element depth above ``MAX_XML_DEPTH`` (256): a refusal, never a
  truncation;
* anything expat reports as malformed, including an undecodable encoding (encoding as declared, default UTF-8).
"""
from __future__ import annotations

import re
import xml.parsers.expat

MAX_XML_BYTES = 8 * 1024 * 1024
MAX_XML_DEPTH = 256
_DECL = re.compile(rb"<!")


class XmlRefused(ValueError):
    """The blob is not acceptable XML; ``reason`` is a fixed code (never blob content)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _prescan(data: bytes) -> None:
    for m in _DECL.finditer(data):
        tail = data[m.start():m.start() + 9]
        if tail.startswith(b"<!--") or tail.startswith(b"<![CDATA["):
            continue
        raise XmlRefused("declaration_not_allowed")
    # A document in a wide encoding hides "<!" from a byte scan: allow only encodings where ASCII is ASCII.
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00<", b"<\x00")):
        raise XmlRefused("encoding_not_allowed")


def check_xml(data: bytes) -> None:
    """Raise ``XmlRefused`` unless ``data`` is a well-formed XML document within the caps."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("blob bytes required")
    if len(data) > MAX_XML_BYTES:
        raise XmlRefused("too_large")
    _prescan(bytes(data))
    parser = xml.parsers.expat.ParserCreate()
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    depth = 0

    def start(_name, _attrs):
        nonlocal depth
        depth += 1
        if depth > MAX_XML_DEPTH:
            raise XmlRefused("too_deep")

    def end(_name):
        nonlocal depth
        depth -= 1

    def forbidden(*_args):
        raise XmlRefused("declaration_not_allowed")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.UnparsedEntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.NotationDeclHandler = forbidden
    try:
        parser.Parse(bytes(data), True)
    except XmlRefused:
        raise
    except (xml.parsers.expat.ExpatError, ValueError, LookupError, UnicodeError):
        raise XmlRefused("malformed") from None


def is_well_formed(data: bytes) -> bool:
    try:
        check_xml(data)
    except XmlRefused:
        return False
    return True
