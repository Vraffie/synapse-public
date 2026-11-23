"""Operator enablement: which exact package bytes may load, and what they are granted.

``plugins.json`` in the state directory is operator-owned (user-owned, mode 0600, not a symlink).
An entry pins id, version and the package SHA-256; granting anything beyond what the manifest
requested is refused. A plugin that is not listed, or whose bytes changed, does not load.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from synapse.plugins.manifest import KNOWN_CAPABILITIES, PluginManifest

ENABLEMENT_NAME = "plugins.json"
MAX_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EnablementError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Enabled:
    id: str
    version: str
    package_sha256: str
    granted: frozenset[str]


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise EnablementError(f"duplicate key: {key}")
        out[key] = value
    return out


def load_enablement(path: Path) -> dict[str, Enabled]:
    """Missing file means nothing is enabled. A present but unsafe or malformed file raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise EnablementError("enablement file must be a private, user-owned regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise EnablementError("enablement file too large")
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnablementError("enablement file is not valid JSON") from exc
    if not isinstance(doc, dict) or set(doc) != {"v", "plugins"} or doc["v"] != 1 or not isinstance(doc["plugins"], list):
        raise EnablementError("unsupported enablement file shape")
    out: dict[str, Enabled] = {}
    for item in doc["plugins"]:
        if not isinstance(item, dict) or set(item) != {"id", "version", "package_sha256", "granted"}:
            raise EnablementError("malformed enablement entry")
        granted = item["granted"]
        if (not isinstance(item["id"], str) or not isinstance(item["version"], str)
                or not isinstance(item["package_sha256"], str) or not _SHA256.fullmatch(item["package_sha256"])
                or not isinstance(granted, list) or not all(isinstance(g, str) for g in granted)
                or not set(granted) <= KNOWN_CAPABILITIES):
            raise EnablementError("malformed enablement entry")
        if item["id"] in out:
            raise EnablementError("a plugin is enabled twice")
        out[item["id"]] = Enabled(item["id"], item["version"], item["package_sha256"], frozenset(granted))
    return out


def enable_plugin(path: Path, manifest: PluginManifest, package_sha256: str, granted: frozenset[str]) -> None:
    """Record an operator's decision (atomic, 0600). Refuses grants the manifest did not request."""
    if not _SHA256.fullmatch(package_sha256):
        raise EnablementError("package_sha256 must be 64 lowercase hex characters")
    if not frozenset(granted) <= manifest.capabilities:
        raise EnablementError("cannot grant a capability the manifest did not request")
    current = load_enablement(path)
    current[manifest.id] = Enabled(manifest.id, manifest.version, package_sha256, frozenset(granted))
    doc = {"v": 1, "plugins": [{"id": e.id, "version": e.version, "package_sha256": e.package_sha256,
                                 "granted": sorted(e.granted)} for e in sorted(current.values(), key=lambda e: e.id)]}
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".plugins-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(doc, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
