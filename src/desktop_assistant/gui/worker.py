from __future__ import annotations

import logging
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

    @Slot()
    def run(self) -> None:
        try:
            result = self._operation()
        except Exception:
            logger.exception("Unexpected error in a background operation")
            self.signals.failed.emit()
        else:
            self.signals.succeeded.emit(result)


class AssistantWorker(BackgroundWorker):
    """Backward-compatible worker for one assistant request."""

    def __init__(self, handler: Callable[[str], object], command: str) -> None:
        super().__init__(lambda: handler(command))
