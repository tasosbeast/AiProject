from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal()


class BackgroundWorker(QRunnable):
    """Runs one no-argument operation away from the GUI event loop."""

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self._operation = operation
        self.signals = WorkerSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        try:
            self.signals.succeeded.disconnect()
        except RuntimeError:
            pass
        try:
            self.signals.failed.disconnect()
        except RuntimeError:
            pass

    @Slot()
    def run(self) -> None:
        if self._cancelled:
            return
        thread = threading.Thread(target=self._execute, daemon=True)
        thread.start()

    def _execute(self) -> None:
        try:
            result = self._operation()
        except Exception:
            if self._cancelled:
                return
            logger.exception("Unexpected error in a background operation")
            try:
                self.signals.failed.emit()
            except RuntimeError:
                pass
        else:
            if self._cancelled:
                return
            try:
                self.signals.succeeded.emit(result)
            except RuntimeError:
                pass


class AssistantWorker(BackgroundWorker):
    """Backward-compatible worker for one assistant request."""

    def __init__(self, handler: Callable[[str], object], command: str) -> None:
        super().__init__(lambda: handler(command))

