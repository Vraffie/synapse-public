from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gc
import os

import pytest

# Tests must not write synapse-chain-anchor records into the developer's systemd journal (ADR 018).
# A test that needs the journal sink constructs JournalAnchors with a fake socket/runner explicitly.
os.environ["SYNAPSE_DISABLE_JOURNAL_ANCHORS"] = "1"


def _isolate_provider_credentials() -> None:
    """Never let a fake-executable test copy the developer's real CLI login into a scratch dir.

    Both adapters fall back to ``$CODEX_HOME/auth.json`` / ``$CLAUDE_CONFIG_DIR/.credentials.json`` and then to
    ``~/.codex`` / ``~/.claude`` when a test builds a provider with the ambient environment. Point both variables at
    a private directory holding fake files so those candidates are found first and the real home is never read.
    Opt-in real-binary runs (SYNAPSE_REAL_CLAUDE=1 / SYNAPSE_REAL_CODEX=1) keep the real environment.
    """
    import atexit
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="synapse-test-creds-")
    atexit.register(shutil.rmtree, root, True)
    for real_flag, var, name in (("SYNAPSE_REAL_CODEX", "CODEX_HOME", "auth.json"),
                                 ("SYNAPSE_REAL_CLAUDE", "CLAUDE_CONFIG_DIR", ".credentials.json")):
        if os.environ.get(real_flag) == "1":
            continue
        directory = os.path.join(root, var.lower())
        os.mkdir(directory, 0o700)
        fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write('{"fake": "test-credentials"}')
        os.environ[var] = directory


_isolate_provider_credentials()


@pytest.fixture(autouse=True)
def _collect_garbage_on_main_thread(request):
    """Qt widgets must be destroyed on the GUI thread. Windows leaked by one test sit in reference cycles; if the cyclic GC
    first runs on another thread (e.g. the desktop Poller thread) their destructors crash. Collect here, on the test thread."""
    yield
    if request.module.__name__.startswith("test_desktop"):  # only these build Qt windows; a collect per test is slow
        gc.collect()
