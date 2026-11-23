import os
import signal
import subprocess
import sys

from synapse.providers import credential_sweep as cs

DEAD = 2**22 + 12345  # above pid_max default range; faked as dead anyway


def _scratch(base, name, *, pid, boot="old", marker=True, secret=b"s3cret"):
    base.mkdir(mode=0o700, exist_ok=True)
    root = base / name
    root.mkdir(mode=0o700)
    if marker:
        cs.write_marker(root, pid=pid, boot_id=boot)
    cfg = root / "config"
    cfg.mkdir()
    (cfg / ".credentials.json").write_bytes(secret)
    return root


def _dead(pid):
    return False


def test_removes_stale_and_skips_live_own_and_unmarked(tmp_path):
    base = tmp_path / "scratch"
    stale = _scratch(base, "a" * 32, pid=DEAD)
    mine = _scratch(base, "b" * 32, pid=os.getpid(), boot=cs.BOOT_ID)
    unmarked = _scratch(base, "c" * 32, pid=DEAD, marker=False)
    odd = _scratch(base, "not-a-uuid", pid=DEAD)
    removed = cs.sweep_scratch([base], pid_alive=lambda p: p == os.getpid())
    assert removed == [str(stale)]
    assert not stale.exists()
    assert mine.exists() and unmarked.exists() and odd.exists()


def test_live_foreign_process_incarnation_is_skipped(tmp_path):
    base = tmp_path / "scratch"
    other = _scratch(base, "d" * 32, pid=os.getpid(), boot="other-daemon")  # same pid+start time
    assert cs.sweep_scratch([base]) == []
    assert other.exists()


def test_recycled_pid_with_different_start_time_is_removed(tmp_path):
    base = tmp_path / "scratch"
    root = _scratch(base, "e" * 32, pid=os.getpid(), boot="other-daemon")
    marker = root / cs.MARKER_NAME
    marker.write_text(marker.read_text().replace('"start": "', '"start": "9'))
    assert cs.sweep_scratch([base]) == [str(root)]


def test_symlinks_never_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("precious")
    base = tmp_path / "scratch"
    stale = _scratch(base, "a" * 32, pid=DEAD)
    os.symlink(victim, stale / "link-dir")
    os.symlink(outside_file, stale / "link-file")
    # a symlinked entry in the base pointing at a stale-looking dir elsewhere
    target = _scratch(tmp_path / "elsewhere", "f" * 32, pid=DEAD)
    os.symlink(target, base / ("9" * 32))
    assert cs.sweep_scratch([base], pid_alive=_dead) == [str(stale)]
    assert not stale.exists()
    assert (victim / "keep.txt").read_text() == "keep"
    assert outside_file.read_text() == "precious"
    assert target.exists()


def test_symlinked_root_is_ignored(tmp_path):
    real = tmp_path / "real"
    stale = _scratch(real, "a" * 32, pid=DEAD)
    link = tmp_path / "link"
    os.symlink(real, link)
    assert cs.sweep_scratch([link], pid_alive=_dead) == []
    assert stale.exists()


def test_missing_root_ok_and_credentials_overwritten(tmp_path, monkeypatch):
    assert cs.sweep_scratch([tmp_path / "nope"]) == []
    base = tmp_path / "scratch"
    stale = _scratch(base, "a" * 32, pid=DEAD)
    cred = stale / "config" / ".credentials.json"
    seen = []
    real_unlink = os.unlink

    def spy(name, *, dir_fd=None):
        if name == ".credentials.json":
            fd = os.open(name, os.O_RDONLY, dir_fd=dir_fd)
            seen.append(os.read(fd, 100))
            os.close(fd)
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(cs.os, "unlink", spy)
    assert cred.exists()
    cs.sweep_scratch([base], pid_alive=_dead)
    assert seen == [b"\0" * 6]
    assert not stale.exists()


def test_readonly_subdir_is_removed(tmp_path):
    base = tmp_path / "scratch"
    stale = _scratch(base, "a" * 32, pid=DEAD)
    os.chmod(stale / "config", 0o500)
    assert cs.sweep_scratch([base], pid_alive=_dead) == [str(stale)]


def test_sigkill_simulation(tmp_path):
    base = tmp_path / "scratch"
    base.mkdir(mode=0o700)
    code = (
        "import os,sys,time\n"
        "from synapse.providers import credential_sweep as cs\n"
        "root=os.path.join(sys.argv[1],'a'*32)\n"
        "os.mkdir(root,0o700); cs.write_marker(root)\n"
        "open(os.path.join(root,'auth.json'),'w').write('tok')\n"
        "print('ready',flush=True); time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(base)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        root = base / ("a" * 32)
        assert cs.sweep_scratch([base]) == []  # owner alive: untouched
        assert root.exists()
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        assert cs.sweep_scratch([base]) == [str(root)]
        assert not root.exists()
    finally:
        proc.kill()
        proc.stdout.close()


def test_startup_roots_cover_both_adapters(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    roots = cs.scratch_roots(tmp_path / "state")
    assert roots == [str(tmp_path / "tmp" / f"synapse-agent-{os.getuid()}"), str(tmp_path / "state" / "codex-run")]
    a = _scratch(tmp_path / "tmp" / f"synapse-agent-{os.getuid()}", "a" * 32, pid=DEAD)
    b = _scratch(tmp_path / "state" / "codex-run", "b" * 32, pid=DEAD) if (tmp_path / "state").mkdir() is None else None
    assert sorted(cs.sweep_startup(tmp_path / "state")) == sorted([str(a), str(b)])


def test_deep_chain_does_not_raise_or_block_startup(tmp_path):
    base = tmp_path / "scratch"
    stale = _scratch(base, "a" * 32, pid=DEAD)
    fd = os.open(stale, os.O_RDONLY | os.O_DIRECTORY)
    try:
        cur = fd
        for _ in range(3000):
            os.mkdir("d", dir_fd=cur)
            nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=cur)
            if cur != fd:
                os.close(cur)
            cur = nxt
        os.close(cur)
    finally:
        os.close(fd)
    assert cs.sweep_scratch([base], pid_alive=_dead) == []  # no RecursionError; too deep is left in place
    assert stale.exists()


def test_sweep_startup_never_raises(tmp_path, monkeypatch):
    def boom(roots):
        raise RuntimeError("boom")
    monkeypatch.setattr(cs, "sweep_scratch", boom)
    assert cs.sweep_startup(tmp_path) == []


def test_large_or_noncredential_files_are_not_zero_filled(tmp_path, monkeypatch):
    base = tmp_path / "scratch"
    root = _scratch(base, "a" * 32, pid=DEAD)
    (root / "big-auth.json").write_bytes(b"x" * (cs.MAX_SHRED_BYTES + 1))
    (root / "notes.txt").write_bytes(b"hello")
    writes = []
    real = os.write
    monkeypatch.setattr(cs.os, "write", lambda fd, data: writes.append(len(data)) or real(fd, data))
    assert cs.sweep_scratch([base], pid_alive=_dead) == [str(root)]
    assert sum(writes) == len(b"s3cret")  # only the small credential-named file was overwritten
