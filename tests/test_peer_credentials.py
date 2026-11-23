import os
import socket
import threading

import pytest

from synapse.daemon import peer_credentials as pc


def test_socketpair_reports_own_ids():
    a, b = socket.socketpair()
    with a, b:
        for s in (a, b):
            assert pc.peer_credentials(s) == (os.getpid(), os.geteuid(), os.getegid())


def test_real_unix_server_sees_client_uid(tmp_path):
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    result = []

    def client():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(path)
            result.append(pc.peer_credentials(c))

    t = threading.Thread(target=client)
    t.start()
    conn, _ = srv.accept()
    with conn, srv:
        pid, uid, gid = pc.peer_credentials(conn)
        t.join()
    assert uid == os.geteuid() and gid == os.getegid() and pid == os.getpid()
    assert result[0][1] == os.geteuid()


def test_tcp_socket_is_rejected():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(pc.PeerCredentialsUnavailable):
            pc.peer_credentials(s)


def test_unsupported_platform_raises(monkeypatch):
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    a, b = socket.socketpair()
    with a, b:
        with pytest.raises(pc.PeerCredentialsUnavailable):
            pc.peer_credentials(a)
