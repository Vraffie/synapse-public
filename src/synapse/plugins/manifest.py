"""``manifest.json``: what a plugin package claims to be and asks for. Strictly validated."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from synapse.plugins.safe_text import bounded

MANIFEST_NAME = "manifest.json"
API_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024
_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VERSION = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")
_ENTRY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.py$")

# What a plugin may ask for. Registration rights and privileged actions use one vocabulary so a
# grant is a plain set membership. "deploy.production" is deliberately absent: it is disabled and
# can never be requested, granted or authorised through a plugin (AGENTS.md).
REGISTRATION_CAPABILITIES = frozenset({"register.roles", "register.validators"})
PRIVILEGED_ACTIONS = frozenset({"repo.write", "git.push", "git.merge", "deploy.staging", "approval.request"})
KNOWN_CAPABILITIES = REGISTRATION_CAPABILITIES | PRIVILEGED_ACTIONS
_FIELDS = {"manifest_version", "id", "version", "api_version", "entrypoint", "description", "capabilities"}


class ManifestError(ValueError):
    """The manifest is malformed, oversized or asks for something that does not exist."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    id: str
    version: str
    entrypoint: str
    description: str
    capabilities: frozenset[str]


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ManifestError(f"duplicate manifest key: {bounded(key, 64)}")
        out[key] = value
    return out


def parse_manifest(data: bytes) -> PluginManifest:
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest missing or larger than 16 KiB")
    try:
        doc = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=lambda c: (_ for _ in ()).throw(ManifestError("NaN/Infinity not allowed")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(doc, dict) or set(doc) != _FIELDS:
        raise ManifestError("manifest must have exactly the fields: " + ", ".join(sorted(_FIELDS)))
    if doc["manifest_version"] != 1 or type(doc["manifest_version"]) is not int:
        raise ManifestError("unsupported manifest_version")
    if doc["api_version"] != API_VERSION or type(doc["api_version"]) is not int:
        raise ManifestError(f"unsupported api_version (this core speaks {API_VERSION})")
    for key, pattern in (("id", _ID), ("version", _VERSION), ("entrypoint", _ENTRY)):
        if not isinstance(doc[key], str) or not pattern.fullmatch(doc[key]):
            raise ManifestError(f"invalid {key}")
    if not isinstance(doc["description"], str) or not 0 < len(doc["description"]) <= 400:
        raise ManifestError("description must be 1 to 400 characters")
    caps = doc["capabilities"]
    if not isinstance(caps, list) or len(caps) > 16 or not all(isinstance(c, str) for c in caps) or len(set(caps)) != len(caps):
        raise ManifestError("capabilities must be a list of at most 16 distinct strings")
    unknown = sorted(set(caps) - KNOWN_CAPABILITIES)
    if unknown:
        raise ManifestError("unknown capability: " + bounded(", ".join(unknown[:4]), 160))
    return PluginManifest(doc["id"], doc["version"], doc["entrypoint"], doc["description"], frozenset(caps))
