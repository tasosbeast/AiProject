from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Thread
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from desktop_assistant.gui.single_instance import SingleInstanceCoordinator


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(qt_app: QApplication, predicate: object, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for local IPC")


def test_second_instance_only_activates_primary(qt_app: QApplication) -> None:
    server_name = f"AiProject.tests.{uuid4().hex}"
    activations: list[str] = []
    primary = SingleInstanceCoordinator(server_name=server_name)
    primary.set_activation_callback(lambda: activations.append("show"))

    assert primary.acquire()
    assert primary.is_primary

    secondary_results: list[bool] = []

    def run_secondary() -> None:
        secondary = SingleInstanceCoordinator(server_name=server_name)
        acquired = secondary.acquire()
        secondary_results.append(acquired)
        secondary.shutdown()

    thread = Thread(target=run_secondary)
    thread.start()
    _wait_until(qt_app, lambda: bool(secondary_results))
    thread.join(timeout=1)
    assert secondary_results == [False]
    _wait_until(qt_app, lambda: activations == ["show"])

    primary.shutdown()


def test_primary_shutdown_allows_clean_restart(qt_app: QApplication) -> None:
    server_name = f"AiProject.tests.{uuid4().hex}"
    first = SingleInstanceCoordinator(server_name=server_name)
    assert first.acquire()
    first.shutdown()

    replacement = SingleInstanceCoordinator(server_name=server_name)
    assert replacement.acquire()
    assert replacement.is_primary
    replacement.shutdown()


def test_stale_lock_recovery_when_no_server_running(qt_app: QApplication, tmp_path) -> None:
    from PySide6.QtCore import QLockFile, QStandardPaths
    from PySide6.QtNetwork import QLocalServer

    server_name = f"AiProject.tests.{uuid4().hex}"
    temp_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation))
    lock_file_path = temp_dir / f"{server_name}.lock"

    # Simulate an abandoned lock file from a dead process (e.g. invalid PID)
    lock_file_path.write_text("12345678\nfake-host\nfake-app\n", encoding="utf-8")

    coordinator = SingleInstanceCoordinator(server_name=server_name)
    assert coordinator.acquire()
    assert coordinator.is_primary
    coordinator.shutdown()


def test_listen_failure_fails_safely_and_releases_lock(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    from desktop_assistant.gui.single_instance import SingleInstanceError
    from PySide6.QtNetwork import QLocalServer

    server_name = f"AiProject.tests.{uuid4().hex}"
    coordinator = SingleInstanceCoordinator(server_name=server_name)

    monkeypatch.setattr(coordinator._server, "listen", lambda _name: False)

    with pytest.raises(SingleInstanceError):
        coordinator.acquire()

    assert not coordinator.is_primary
    assert not coordinator._owns_server
    # Lock was unlocked on listen failure, allowing another coordinator to attempt lock
    replacement = SingleInstanceCoordinator(server_name=server_name)
    assert replacement.acquire()
    assert replacement.is_primary
    replacement.shutdown()


def test_exact_show_payload_protocol(qt_app: QApplication) -> None:
    from PySide6.QtNetwork import QLocalSocket

    server_name = f"AiProject.tests.{uuid4().hex}"
    activations: list[str] = []
    primary = SingleInstanceCoordinator(server_name=server_name)
    primary.set_activation_callback(lambda: activations.append("activated"))
    assert primary.acquire()

    def send_raw(payload: bytes | None) -> None:
        done = []

        def _worker() -> None:
            socket = QLocalSocket()
            socket.connectToServer(server_name)
            if socket.waitForConnected(500):
                if payload is not None:
                    socket.write(payload)
                    socket.flush()
                    socket.waitForBytesWritten(500)
                if not socket.waitForDisconnected(500):
                    socket.disconnectFromServer()
                    socket.waitForDisconnected(500)
            done.append(True)

        t = Thread(target=_worker)
        t.start()
        _wait_until(qt_app, lambda: bool(done), timeout=1.0)
        t.join(timeout=1.0)

    # 1. Non-SHOW payload: HELLO
    send_raw(b"HELLO\n")
    _wait_until(qt_app, lambda: True, timeout=0.1)
    assert activations == []

    # 2. Empty payload
    send_raw(None)
    _wait_until(qt_app, lambda: True, timeout=0.1)
    assert activations == []

    # 3. Malformed binary payload
    send_raw(b"\xff\xfe\x00\x01\x02")
    _wait_until(qt_app, lambda: True, timeout=0.1)
    assert activations == []

    # 4. Exact SHOW payload
    send_raw(b"SHOW\n")
    _wait_until(qt_app, lambda: len(activations) == 1)
    assert activations == ["activated"]

    primary.shutdown()


