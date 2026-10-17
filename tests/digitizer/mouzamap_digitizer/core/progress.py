"""Progress reporting and cooperative cancellation for long-running operations."""
from __future__ import annotations

import threading
from typing import Callable, Optional


class OperationCancelled(Exception):
    """Raised inside a worker when the user cancels the current operation."""


class CancelToken:
    """Thread-safe cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class Progress:
    """Reports percentage/message and raises OperationCancelled if cancelled.

    `sub(start, end)` returns a reporter that maps 0-100 onto a slice of the parent range,
    so nested stages can report their own 0-100 progress.
    """

    def __init__(
        self,
        callback: Optional[Callable[[int, str], None]] = None,
        token: Optional[CancelToken] = None,
        _start: float = 0.0,
        _end: float = 100.0,
    ) -> None:
        self._callback = callback
        self._token = token or CancelToken()
        self._start = _start
        self._end = _end

    def check(self) -> None:
        if self._token.cancelled:
            raise OperationCancelled()

    def report(self, percent: float, message: str = "") -> None:
        self.check()
        if self._callback is not None:
            value = self._start + (self._end - self._start) * max(0.0, min(100.0, percent)) / 100.0
            self._callback(int(round(value)), message)

    def sub(self, start: float, end: float) -> "Progress":
        span = self._end - self._start
        return Progress(
            self._callback,
            self._token,
            self._start + span * start / 100.0,
            self._start + span * end / 100.0,
        )
