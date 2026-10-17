"""Background execution so the GUI never freezes during heavy image processing."""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QThread, Signal

from core.progress import CancelToken, OperationCancelled, Progress

log = logging.getLogger(__name__)


class TaskThread(QThread):
    """Runs `fn(progress)` on a worker thread.

    Signals are delivered to the GUI thread: `progress(percent, message)`, then exactly one of
    `result(object)`, `error(message)` or `cancelled()`.
    """

    progress = Signal(int, str)
    result = Signal(object)
    error = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: Callable[[Progress], object], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._token = CancelToken()

    def cancel(self) -> None:
        self._token.cancel()

    def run(self) -> None:
        prog = Progress(lambda p, m: self.progress.emit(p, m), self._token)
        try:
            self.result.emit(self._fn(prog))
        except OperationCancelled:
            self.cancelled.emit()
        except MemoryError:
            self.error.emit("Out of memory. Try a lower render DPI or a smaller crop.")
        except Exception as exc:  # noqa: BLE001 - reported to the user
            log.exception("Background task failed")
            self.error.emit(str(exc) or type(exc).__name__)
