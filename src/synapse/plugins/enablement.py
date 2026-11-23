"""Operator enablement: which exact package bytes may load, and what they are granted.

``plugins.json`` in the state directory is operator-owned (user-owned, mode 0600, not a symlink).
An entry pins id, version and the package SHA-256; granting anything beyond what the manifest
requested is refused. A plugin that is not listed, or whose bytes changed, does not load.

Same-uid assumption: the daemon, the operator and every enabled plugin run as the same user, so an
enabled plugin can rewrite this file (for example to raise grants for the next start), and one that runs in the host process can also edit the
host's in-memory copy of the entries, which takes effect at once and can raise ANOTHER plugin's grants. That
cannot be prevented in-process. What this module offers is *detection by the operator*, not a
boundary: ``enable_plugin`` returns the SHA-256 of the exact file it wrote and ``read_enablement``
returns the SHA-256 of the exact bytes it read, so a host or operator that kept the digest somewhere
a plugin cannot write can see a silent edit. A digest kept in the same user's files is only as
trustworthy as those files.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256 as _sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from synapse.plugins.manifest import KNOWN_CAPABILITIES, MANIFEST_NAME, PluginManifest, parse_manifest
from synapse.plugins.package import snapshot_package

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


@dataclass(frozen=True, slots=True)
class EnablementFile:
    entries: dict[str, Enabled]
    sha256: str | None  # of the exact bytes read; None when the file does not exist


def load_enablement(path: Path) -> dict[str, Enabled]:
    """Missing file means nothing is enabled. A present but unsafe or malformed file raises."""
    return read_enablement(path).entries


def read_enablement(path: Path) -> EnablementFile:
    """Like ``load_enablement`` and also the SHA-256 of the bytes read (compare with ``enable_plugin``'s)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return EnablementFile({}, None)
    except OSError as exc:  # e.g. ELOOP for a symlink: refuse with the module's own error
        raise EnablementError("enablement file must be a private, user-owned regular file") from exc
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
    if not isinstance(doc, dict) or set(doc) != {"v", "plugins"} or type(doc["v"]) is not int or doc["v"] != 1 or not isinstance(doc["plugins"], list):
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
    return EnablementFile(out, _sha256(data).hexdigest())


def enable_package(path: Path, package_dir: Path, reviewed_sha256: str, granted: frozenset[str]) -> str:
    """Operator path: hash the package directory now and enable it only if it is the one reviewed.

    ``reviewed_sha256`` is the value the operator read from ``python -m synapse.plugins hash`` when
    reviewing the code. If the directory no longer hashes to it, nothing is recorded. Returns the
    SHA-256 of the enablement file that was written.
    """
    package = snapshot_package(Path(package_dir))
    manifest = parse_manifest(package.files.get(MANIFEST_NAME, b""))
    if package.sha256 != reviewed_sha256:
        raise EnablementError("the package on disk is not the one that was reviewed (hash differs)")
    if Path(package_dir).name != manifest.id:
        raise EnablementError("the package directory must be named after the plugin id")
    return enable_plugin(path, manifest, package.sha256, granted)


def enable_plugin(path: Path, manifest: PluginManifest, package_sha256: str, granted: frozenset[str]) -> str:
    """Record an operator's decision (atomic, 0600). Refuses grants the manifest did not request.

    Low level: the caller vouches that ``package_sha256`` belongs to ``manifest``'s package; prefer
    ``enable_package``, which computes it. Returns the SHA-256 of the file written.
    """
    path = Path(path)
    if not _SHA256.fullmatch(package_sha256):
        raise EnablementError("package_sha256 must be 64 lowercase hex characters")
    if not frozenset(granted) <= manifest.capabilities:
        raise EnablementError("cannot grant a capability the manifest did not request")
    current = load_enablement(path)
    current[manifest.id] = Enabled(manifest.id, manifest.version, package_sha256, frozenset(granted))
    doc = {"v": 1, "plugins": [{"id": e.id, "version": e.version, "package_sha256": e.package_sha256,
                                 "granted": sorted(e.granted)} for e in sorted(current.values(), key=lambda e: e.id)]}
    data = json.dumps(doc, sort_keys=True).encode("utf-8")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".plugins-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)  # make the rename itself durable before the digest is reported
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    return _sha256(data).hexdigest()
