"""``python -m synapse.plugins hash DIR``: print what an operator needs to pin a package."""
from __future__ import annotations

from pathlib import Path
import sys

from synapse.plugins.manifest import MANIFEST_NAME, parse_manifest
from synapse.plugins.package import snapshot_package


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "hash":
        print("usage: python -m synapse.plugins hash PACKAGE_DIR", file=sys.stderr)
        return 2
    try:
        package = snapshot_package(Path(argv[1]))
        manifest = parse_manifest(package.files.get(MANIFEST_NAME, b""))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"id={manifest.id} version={manifest.version} package_sha256={package.sha256}")
    print("requested capabilities: " + (", ".join(sorted(manifest.capabilities)) or "(none)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
