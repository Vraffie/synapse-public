from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gc
import os

import pytest

# Tests must not write synapse-chain-anchor records into the developer's systemd journal (ADR 018).
# A test that needs the journal sink constructs JournalAnchors with a fake socket/runner explicitly.
os.environ["SYNAPSE_DISABLE_JOURNAL_ANCHORS"] = "1"


# --- provider credential isolation -----------------------------------------------------------------------------
# Both adapters look for a login at $CODEX_HOME/auth.json / $CLAUDE_CONFIG_DIR/.credentials.json and then under
# ~/.codex / ~/.claude of whatever HOME the provider's environment names. Fake-executable tests must never read the
# developer's real login, so:
#   1. CODEX_HOME and CLAUDE_CONFIG_DIR point at private fake files for the whole session (found first);
#   2. every test except one marked ``real_cli(FLAG)`` with FLAG=1 runs with both adapters' credential readers
#      guarded: a read of a real location (even through an explicit environ naming the real HOME) is refused before
#      the file is opened, and the test fails.
# Only tests marked ``real_cli`` see the real environment, so SYNAPSE_REAL_*=1 never weakens the other tests of the
# session (running the real tests in a separate invocation is still recommended).
_ORIGINAL_ENV = {var: os.environ.get(var) for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR")}


def _protected_credential_roots() -> tuple[str, ...]:
    import pwd
    homes = {pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME") or ""}
    roots = [os.path.join(h, d) for h in homes if h for d in (".codex", ".claude")]
    roots += [v for v in _ORIGINAL_ENV.values() if v]
    return tuple(sorted({os.path.realpath(r) for r in roots}))


PROTECTED_CREDENTIAL_ROOTS = _protected_credential_roots()


def _fake_credential_dirs() -> dict[str, str]:
    import atexit
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="synapse-test-creds-")
    atexit.register(shutil.rmtree, root, True)
    dirs = {}
    for var, name in (("CODEX_HOME", "auth.json"), ("CLAUDE_CONFIG_DIR", ".credentials.json")):
        directory = os.path.join(root, var.lower())
        os.mkdir(directory, 0o700)
        fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write('{"fake": "test-credentials"}')
        dirs[var] = directory
    return dirs


FAKE_CREDENTIAL_DIRS = _fake_credential_dirs()
os.environ.update(FAKE_CREDENTIAL_DIRS)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_cli(flag): opt-in real-binary test; sees the real environment when flag=1")


def is_protected_credential_path(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep) for r in PROTECTED_CREDENTIAL_ROOTS)


@pytest.fixture(autouse=True)
def credential_guard(request, monkeypatch):
    """Refused real-credential reads of this test; the test fails at teardown unless it empties the list itself."""
    marker = request.node.get_closest_marker("real_cli")
    if marker is not None and marker.args and os.environ.get(marker.args[0]) == "1":
        for var, value in _ORIGINAL_ENV.items():  # the real login, for this opted-in test only
            if value is None:
                monkeypatch.delenv(var, raising=False)
            else:
                monkeypatch.setenv(var, value)
        yield []
        return
    from synapse.providers.claude_cli import ClaudeCliProvider
    from synapse.providers.codex_cli import CodexCliProvider
    refused: list[str] = []
    for cls in (ClaudeCliProvider, CodexCliProvider):
        def guarded(src, _original=cls._read_credentials):
            if is_protected_credential_path(src):
                refused.append(src)
                raise PermissionError("test isolation: the real CLI login is never read by tests")
            return _original(src)
        monkeypatch.setattr(cls, "_read_credentials", staticmethod(guarded))
    for var, value in FAKE_CREDENTIAL_DIRS.items():
        monkeypatch.setenv(var, value)
    yield refused
    if refused:
        pytest.fail("a test tried to read a real CLI credential file: %s" % sorted(set(refused)))


@pytest.fixture(autouse=True)
def _collect_garbage_on_main_thread(request):
    """Qt widgets must be destroyed on the GUI thread. Windows leaked by one test sit in reference cycles; if the cyclic GC
    first runs on another thread (e.g. the desktop Poller thread) their destructors crash. Collect here, on the test thread."""
    yield
    if request.module.__name__.startswith("test_desktop"):  # only these build Qt windows; a collect per test is slow
        gc.collect()
