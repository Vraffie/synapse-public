"""Race-oriented tests: reclaim operates through a verified directory fd."""
import errno
import os
import socket

import pytest

from synapse.infrastructure import socket_reclaim
from synapse.infrastructure.socket_reclaim import reclaim_stale_socket


def _bound(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    return s


def test_stale_socket_is_reclaimed(tmp_path):
    s = _bound(tmp_path / 'a.sock')
    try:
        assert reclaim_stale_socket(tmp_path / 'a.sock') is True
        assert not (tmp_path / 'a.sock').exists()
    finally:
        s.close()


def test_live_socket_untouched(tmp_path):
    s = _bound(tmp_path / 'a.sock')
    s.listen(1)
    try:
        ino = (tmp_path / 'a.sock').lstat().st_ino
        with pytest.raises(OSError) as e:
            reclaim_stale_socket(tmp_path / 'a.sock')
        assert e.value.errno == errno.EADDRINUSE
        assert (tmp_path / 'a.sock').lstat().st_ino == ino
    finally:
        s.close()


def test_missing_directory_is_not_an_error(tmp_path):
    assert reclaim_stale_socket(tmp_path / 'nodir' / 'a.sock') is False


def test_foreign_owned_socket_untouched(tmp_path, monkeypatch):
    s = _bound(tmp_path / 'a.sock')
    try:
        monkeypatch.setattr(os, 'geteuid', lambda: os.getuid() + 12345)
        # directory now also looks foreign; either refusal must leave the socket.
        with pytest.raises(OSError) as e:
            reclaim_stale_socket(tmp_path / 'a.sock')
        assert e.value.errno == errno.EADDRINUSE
        assert (tmp_path / 'a.sock').exists()
    finally:
        s.close()


def test_foreign_owned_socket_in_trusted_dir_untouched(tmp_path, monkeypatch):
    s = _bound(tmp_path / 'a.sock')
    real = os.lstat

    def fake(name, *a, **k):
        r = real(name, *a, **k)
        return os.stat_result((r.st_mode, r.st_ino, r.st_dev, r.st_nlink, r.st_uid + 1, *r[5:]))

    try:
        monkeypatch.setattr(socket_reclaim.os, 'lstat', fake)
        with pytest.raises(OSError) as e:
            reclaim_stale_socket(tmp_path / 'a.sock')
        assert e.value.errno == errno.EADDRINUSE
        assert (tmp_path / 'a.sock').exists()
    finally:
        s.close()


def test_group_writable_directory_refused(tmp_path):
    d = tmp_path / 'shared'
    d.mkdir()
    s = _bound(d / 'a.sock')
    try:
        d.chmod(0o777)
        with pytest.raises(OSError) as e:
            reclaim_stale_socket(d / 'a.sock')
        assert e.value.errno == errno.EADDRINUSE
        assert (d / 'a.sock').exists()
    finally:
        d.chmod(0o700)
        s.close()


def test_non_socket_untouched(tmp_path):
    f = tmp_path / 'a.sock'
    f.write_text('x')
    with pytest.raises(OSError):
        reclaim_stale_socket(f)
    assert f.read_text() == 'x'


def test_parent_swap_after_open_does_not_affect_other_dir(tmp_path, monkeypatch):
    """Swap the parent for a symlink to a victim dir after the dir fd is opened."""
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    victim = tmp_path / 'victim'
    victim.mkdir(mode=0o700)
    stale = _bound(state / 'a.sock')
    victim_file = victim / 'a.sock'
    victim_file.write_text('precious')
    real_open = socket_reclaim._open_verified_dir

    def open_then_swap(parent, path):
        fd = real_open(parent, path)
        moved = tmp_path / 'state-moved'
        os.rename(state, moved)
        os.symlink(victim, state)
        return fd

    monkeypatch.setattr(socket_reclaim, '_open_verified_dir', open_then_swap)
    try:
        # The fd still refers to the original directory: the stale socket is
        # reclaimed there and the victim's file is never touched.
        assert reclaim_stale_socket(state / 'a.sock') is True
        assert victim_file.read_text() == 'precious'
        assert not (tmp_path / 'state-moved' / 'a.sock').exists()
    finally:
        stale.close()


def test_file_swapped_during_probe_is_not_unlinked(tmp_path, monkeypatch):
    path = tmp_path / 'a.sock'
    stale = _bound(path)
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    real_target = socket_reclaim._probe_target

    def swap(dir_fd, name, p):
        target = real_target(dir_fd, name, p)
        os.unlink(path)
        other.bind(str(path))  # replacement endpoint with a new inode
        return target

    monkeypatch.setattr(socket_reclaim, '_probe_target', swap)
    try:
        with pytest.raises(OSError) as e:
            reclaim_stale_socket(path)
        assert e.value.errno == errno.EADDRINUSE
        assert path.exists()
    finally:
        stale.close()
        other.close()


def test_symlink_swapped_in_for_socket_is_not_followed_or_unlinked(tmp_path, monkeypatch):
    path = tmp_path / 'a.sock'
    stale = _bound(path)
    target_file = tmp_path / 'keep'
    target_file.write_text('keep')
    real_target = socket_reclaim._probe_target

    def swap(dir_fd, name, p):
        t = real_target(dir_fd, name, p)
        os.unlink(path)
        os.symlink(target_file, path)
        return t

    monkeypatch.setattr(socket_reclaim, '_probe_target', swap)
    try:
        with pytest.raises(OSError):
            reclaim_stale_socket(path)
        assert path.is_symlink() and target_file.read_text() == 'keep'
    finally:
        stale.close()
