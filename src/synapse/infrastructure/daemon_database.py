"""Own a private SQLite database for the lifetime of one local daemon."""
from collections.abc import Iterator
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat

from synapse.infrastructure.daemon_lock import daemon_lock
from synapse.persistence.migrations import apply_migrations, open_database


def check_private_database(database: str | Path, *, create: bool = False) -> None:
    """Refuse unless ``database`` is a user-owned, private (no group/other bits) regular file with one link, opened
    without following a symlink. The daemon runs it at startup and before every extra connection it opens."""
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(database, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError("Database must be a user-owned, private regular file with one link")
    finally:
        os.close(fd)


@contextmanager
def daemon_database(path: str | Path, migrations: Path) -> Iterator[sqlite3.Connection]:
    """Acquire daemon ownership, open and migrate SQLite, then release on close.

    The database directory must exist and be private. All daemon processes for
    one database must use the same path. This function does not fence workers.
    """
    requested = Path(path)
    if str(path) == ":memory:" or not requested.name:
        raise ValueError("A persistent database path is required")
    database = requested.parent.resolve(strict=True) / requested.name
    lock_path = database.with_name(database.name + ".lock")
    with daemon_lock(lock_path):
        check_private_database(database, create=True)
        with closing(open_database(database)) as connection:
            apply_migrations(connection, migrations)
            yield connection
