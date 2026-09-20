from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket


logger = logging.getLogger(__name__)
INSTANCE_SERVER_NAME = "AiProject.AIAssistant.v1"


class SingleInstanceCoordinator(QObject):
    """Owns a local IPC endpoint used only to focus the primary GUI instance."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        server_name: str = INSTANCE_SERVER_NAME,
        connect_timeout_ms: int = 350,
    ) -> None:
        super().__init__(parent)
        self._server_name = server_name
        self._connect_timeout_ms = connect_timeout_ms
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        lock_directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation))
        self._lock = QLockFile(str(lock_directory / f"{server_name}.lock"))
        self._activation_callback: Callable[[], None] | None = None
        self._owns_server = False

    @property
    def is_primary(self) -> bool:
        return self._owns_server

    def set_activation_callback(self, callback: Callable[[], None]) -> None:
        self._activation_callback = callback

    def acquire(self) -> bool:
        """Return True for the primary instance, False after notifying it."""

        if self._lock.tryLock(0):
            return self._start_primary_server()
        if self._notify_primary():
            logger.info("Existing assistant instance was asked to show")
            return False

        # Recover only when the owner is demonstrably stale and no server answered.
        if self._lock.removeStaleLockFile() and self._lock.tryLock(0):
            logger.info("Recovered stale single-instance lock")
            return self._start_primary_server()

        logger.warning("Another instance owns the startup lock but did not answer")
        return False

    def shutdown(self) -> None:
        if not self._owns_server:
            return
        self._owns_server = False
        self._server.close()
        QLocalServer.removeServer(self._server_name)
        self._lock.unlock()
        logger.info("Single-instance endpoint stopped")

    def _start_primary_server(self) -> bool:
        QLocalServer.removeServer(self._server_name)
        if self._server.listen(self._server_name):
            self._owns_server = True
            logger.info("Single-instance endpoint started")
            return True
        self._lock.unlock()
        logger.warning("Single-instance endpoint could not be established")
        # Fail open so an IPC platform error does not make the application unusable.
        return True

    def _notify_primary(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(self._connect_timeout_ms):
            socket.abort()
            return False
        socket.disconnectFromServer()
        return True

    @Slot()
    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            socket.disconnectFromServer()
            socket.deleteLater()
            if self._activation_callback is not None:
                self._activation_callback()
