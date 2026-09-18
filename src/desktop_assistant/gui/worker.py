from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from desktop_assistant.models import ToolResult


logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal()


class AssistantWorker(QRunnable):
    """Runs one assistant request away from the GUI event loop."""

    def __init__(self, handler: Callable[[str], ToolResult], command: str) -> None:
        super().__init__()
        self._handler = handler
        self._command = command
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self._handler(self._command)
        except Exception:
            logger.exception("Unexpected error while processing an assistant command")
            self.signals.failed.emit()
        else:
            self.signals.succeeded.emit(result)
