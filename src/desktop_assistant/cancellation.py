from __future__ import annotations

import threading


class CancellationToken:
    """Thread-safe cancellation token owned by the application."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()
