"""Operator commands for plugin packages.

``python -m synapse.plugins hash DIR``: print what an operator needs to pin a package.
``python -m synapse.plugins enable PLUGINS_JSON DIR REVIEWED_SHA256 [CAPABILITY ...]``: hash DIR now,
enable it only if it still matches the reviewed hash, and print the enablement file's new SHA-256.
``python -m synapse.plugins digest PLUGINS_JSON``: print the enablement file's current SHA-256 and
entries, to compare with the digest the operator recorded (see docs/operations/plugin-framework.md).

Run these with ``python -I -m synapse.plugins ...`` from a trusted directory, never from inside an
unreviewed package: Python puts the current directory first on the import path, so a planted ``json.py``
there would run before anything is hashed.
"""
from __future__ import annotations

from pathlib import Path
import sys

from synapse.plugins.enablement import enable_package, read_enablement
from synapse.plugins.manifest import MANIFEST_NAME, parse_manifest
from synapse.plugins.package import snapshot_package
from synapse.plugins.safe_text import bounded

USAGE = ("usage: python -m synapse.plugins hash PACKAGE_DIR\n"
         "       python -m synapse.plugins enable PLUGINS_JSON PACKAGE_DIR REVIEWED_SHA256 [CAPABILITY ...]\n"
         "       python -m synapse.plugins digest PLUGINS_JSON")


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "hash":
            package = snapshot_package(Path(argv[1]))
            manifest = parse_manifest(package.files.get(MANIFEST_NAME, b""))
            print(f"id={manifest.id} version={manifest.version} package_sha256={package.sha256}")
            print("requested capabilities: " + (", ".join(sorted(manifest.capabilities)) or "(none)"))
            return 0
        if len(argv) >= 4 and argv[0] == "enable":
            digest = enable_package(Path(argv[1]), Path(argv[2]), argv[3], frozenset(argv[4:]))
            print(f"enabled; plugins.json sha256={digest} (record this somewhere plugins cannot write)")
            return 0
        if len(argv) == 2 and argv[0] == "digest":
            current = read_enablement(Path(argv[1]))
            print(f"plugins.json sha256={current.sha256 or '(no file)'}")
            for entry in sorted(current.entries.values(), key=lambda e: e.id):
                print(f"{bounded(entry.id)} {bounded(entry.version)} {entry.package_sha256} "
                      f"granted={','.join(sorted(entry.granted)) or '(none)'}")
            return 0
    except (ValueError, OSError) as exc:
        print(f"error: {bounded(str(exc), 300)}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
