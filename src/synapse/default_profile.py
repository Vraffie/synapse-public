"""The operator's default Claude account profile, recorded by ``scripts/synapse_up.py`` as ``<state dir>/default-profile``.

The daemon does not read it and its semantics are unchanged: a request that names no profile still means the built-in
``default`` profile there. Clients (agentctl, the desktop picker) use it to *name* a profile when the operator did not.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

FILE_NAME = "default-profile"
_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def read_default_profile(state_dir: str | os.PathLike) -> str | None:
    """The profile id, or None when there is no usable file (absent, a symlink, not ours, writable by others, malformed)."""
    path = Path(state_dir) / FILE_NAME
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 128:
            return None
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if _ID.fullmatch(text) and text != "default" and not text.startswith("synapse.builtin.") else None
