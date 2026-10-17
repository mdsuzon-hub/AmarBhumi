#!/usr/bin/env python3
"""MouzaMap Digitizer - entry point."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def setup_logging() -> str:
    folder = os.path.join(os.path.expanduser("~"), ".cache", "mouzamap-digitizer")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "app.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
              logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)
    return path


def main() -> int:
    log_path = setup_logging()
    log = logging.getLogger("main")
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 1
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("PySide6 is not installed. Run ./install.sh first.", file=sys.stderr)
        return 1
    from core.version import APP_NAME
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    def excepthook(exc_type, exc, tb):
        log.error("Unhandled exception", exc_info=(exc_type, exc, tb))
        QMessageBox.critical(None, "Unexpected error", f"{exc_type.__name__}: {exc}\n\nDetails: {log_path}")

    sys.excepthook = excepthook
    win = MainWindow()
    win.show()
    for arg in sys.argv[1:]:
        if arg.endswith(".mouzamap"):
            win.open_project(arg)
        elif os.path.isfile(arg):
            win.open_source(arg)
        break
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
