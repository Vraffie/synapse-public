"""Native operator view over the private local daemon socket.

Entry point only; the console lives in connection.py (poller), main_window.py and the *_view.py modules.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtWidgets import QApplication

from synapse.desktop.connection import MAX_BACKOFF, Poller, backoff_delay  # noqa: F401  (stable import path)
from synapse.desktop.events_view import MAX_EVENT_ROWS  # noqa: F401
from synapse.desktop.main_window import MainWindow
from synapse.desktop.theme import apply_theme


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Synapse local operator desktop")
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow(args.state_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
