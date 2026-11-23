"""Peer credentials of a connected AF_UNIX socket (ADR 019, first slice item 4).

Prototype helper only: nothing in the daemon calls it yet. The kernel records
the (pid, uid, gid) of the process that called connect() (or socketpair()).
The uid and gid are trustworthy for an authorisation decision; the pid is
racy (it can be reused after the peer exits) and must never authorise.
"""
from __future__ import annotations

import socket
import struct

# struct ucred { pid_t pid; uid_t uid; gid_t gid; } -- three native ints on Linux.
_UCRED = struct.Struct("3i")


class PeerCredentialsUnavailable(RuntimeError):
    """SO_PEERCRED is not supported here or the socket is not a connected AF_UNIX socket."""


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """Return (pid, uid, gid) of the peer of a connected AF_UNIX socket.

    Raises PeerCredentialsUnavailable on platforms without SO_PEERCRED (for
    example macOS or Windows) or when the socket is not a connected AF_UNIX
    socket. Never returns a guessed value.
    """
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None or not hasattr(socket, "AF_UNIX"):
        raise PeerCredentialsUnavailable("SO_PEERCRED is not supported on this platform")
    if sock.family != socket.AF_UNIX:
        raise PeerCredentialsUnavailable("peer credentials need an AF_UNIX socket")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, _UCRED.size)
    except OSError as exc:
        raise PeerCredentialsUnavailable(f"SO_PEERCRED failed: {exc.strerror or exc}") from exc
    if len(raw) != _UCRED.size:
        raise PeerCredentialsUnavailable("unexpected SO_PEERCRED result size")
    pid, uid, gid = _UCRED.unpack(raw)
    return pid, uid, gid
