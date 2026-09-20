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
    secondary = SingleInstanceCoordinator(server_name=server_name)

    assert primary.acquire()
    assert primary.is_primary
    secondary_results: list[bool] = []
    thread = Thread(target=lambda: secondary_results.append(secondary.acquire()))
    thread.start()
    _wait_until(qt_app, lambda: bool(secondary_results))
    thread.join(timeout=1)
    assert secondary_results == [False]
    assert not secondary.is_primary
    _wait_until(qt_app, lambda: activations == ["show"])

    secondary.shutdown()
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

