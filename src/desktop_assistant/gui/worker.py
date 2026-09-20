from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from desktop_assistant.cancellation import CancellationToken

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal()


class NetworkWorker(QRunnable):
    """Runs an abandonable network-only operation (e.g. transcription, TTS, intent resolution).

    Uses a daemon thread so that if the user quits, process exit is not blocked by slow network I/O.
    Late results are discarded and will never execute tools or mutate destroyed UI.
    """

    def __init__(
        self,
        operation: Callable[[], object],
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        super().__init__()
        self._operation = operation
        self.cancellation_token = cancellation_token or CancellationToken()
        self.signals = WorkerSignals()

    def cancel(self) -> None:
        self.cancellation_token.cancel()

    @Slot()
    def run(self) -> None:
        if self.cancellation_token.is_cancelled:
            return
        thread = threading.Thread(target=self._execute, daemon=True)
        thread.start()

    def _execute(self) -> None:
        try:
            result = self._operation()
        except Exception:
            if self.cancellation_token.is_cancelled:
                return
            logger.exception("Unexpected error in background network operation")
            try:
                self.signals.failed.emit()
            except RuntimeError:
                pass
        else:
            if self.cancellation_token.is_cancelled:
                return
            try:
                self.signals.succeeded.emit(result)
            except RuntimeError:
                pass


class ActionWorker(QRunnable):
    """Runs an authorized local must-finish action (e.g. Assistant.confirm, filesystem mutations).

    Uses a non-daemon thread so that interpreter shutdown does not terminate local operations
    mid-mutation. Shutdown coordinates with wait_completion() to allow bounded time to finish.
    """

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self._operation = operation
        self.signals = WorkerSignals()
        self._thread: threading.Thread | None = None
        self._completed = threading.Event()
        self._disconnected = False

    def wait_completion(self, timeout: float = 2.0) -> bool:
        """Wait up to timeout seconds for this action to complete."""
        self._disconnected = True
        if self._thread is not None and self._thread.is_alive():
            return self._completed.wait(timeout)
        return True

    @Slot()
    def run(self) -> None:
        self._thread = threading.Thread(target=self._execute, daemon=False)
        self._thread.start()

    def _execute(self) -> None:
        try:
            result = self._operation()
        except Exception:
            self._completed.set()
            if self._disconnected:
                return
            logger.exception("Unexpected error in must-finish action operation")
            try:
                self.signals.failed.emit()
            except RuntimeError:
                pass
        else:
            self._completed.set()
            if self._disconnected:
                return
            try:
                self.signals.succeeded.emit(result)
            except RuntimeError:
                pass


# Backward-compatibility alias
BackgroundWorker = NetworkWorker


import inspect

class AssistantWorker(NetworkWorker):
    """Runs an assistant routing request with an explicit cancellation token."""

    def __init__(
        self,
        handler: Callable[..., object],
        command: str,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        token = cancellation_token or CancellationToken()

        def _call() -> object:
            try:
                sig = inspect.signature(handler)
                if "cancellation_token" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    return handler(command, cancellation_token=token)
                return handler(command)
            except (ValueError, TypeError):
                pass
            try:
                return handler(command, cancellation_token=token)
            except TypeError:
                return handler(command)

        super().__init__(
            _call,
            cancellation_token=token,
        )
