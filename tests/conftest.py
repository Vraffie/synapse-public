from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gc
import os

import pytest

# Tests must not write synapse-chain-anchor records into the developer's systemd journal (ADR 018).
# A test that needs the journal sink constructs JournalAnchors with a fake socket/runner explicitly.
os.environ["SYNAPSE_DISABLE_JOURNAL_ANCHORS"] = "1"


@pytest.fixture(autouse=True)
def _collect_garbage_on_main_thread(request):
    """Qt widgets must be destroyed on the GUI thread. Windows leaked by one test sit in reference cycles; if the cyclic GC
    first runs on another thread (e.g. the desktop Poller thread) their destructors crash. Collect here, on the test thread."""
    yield
    if request.module.__name__.startswith("test_desktop"):  # only these build Qt windows; a collect per test is slow
        gc.collect()
