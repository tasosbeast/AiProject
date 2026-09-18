from __future__ import annotations

import os
import time
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_assistant.gui.main_window import MainWindow
from desktop_assistant.gui.widgets import MessageKind
from desktop_assistant.models import RiskLevel, ToolResult


class FakeAssistant:
    def __init__(self, result: ToolResult | None = None, error: Exception | None = None) -> None:
        self.result = result or ToolResult(True, "Opening Spotify.", RiskLevel.SAFE)
        self.error = error
        self.calls: list[str] = []

    def handle(self, request: str) -> ToolResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class BlockingAssistant(FakeAssistant):
    def __init__(self) -> None:
        super().__init__()
        self.release = Event()

    def handle(self, request: str) -> ToolResult:
        self.calls.append(request)
        self.release.wait(timeout=2)
        return self.result


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_until(qt_app: QApplication, predicate: object, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for the GUI operation to finish")


def make_window(assistant: FakeAssistant) -> MainWindow:
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    return MainWindow(assistant, pool)


def test_blank_submission_does_nothing(qt_app: QApplication) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    window.command_input.setPlainText("   \n")

    window.submit_command()
    qt_app.processEvents()

    assert assistant.calls == []
    assert window.conversation.messages == []
    assert not window.is_processing


def test_submission_calls_assistant_once_and_displays_result(qt_app: QApplication) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    window.command_input.setPlainText("open spotify")

    window.submit_command()
    wait_until(qt_app, lambda: not window.is_processing)

    assert assistant.calls == ["open spotify"]
    assert [message.text for message in window.conversation.messages] == [
        "open spotify",
        "Opening Spotify.",
    ]
    assert window.conversation.messages[-1].kind is MessageKind.ASSISTANT
    assert window.send_button.isEnabled()
    assert window.command_input.isEnabled()
    assert window.status_label.text() == "Ready"


def test_rejected_result_uses_error_presentation(qt_app: QApplication) -> None:
    result = ToolResult(False, "I did not understand that command.", RiskLevel.SAFE)
    window = make_window(FakeAssistant(result=result))
    window.command_input.setPlainText("do something unsafe")

    window.submit_command()
    wait_until(qt_app, lambda: not window.is_processing)

    assert window.conversation.messages[-1].text == result.message
    assert window.conversation.messages[-1].kind is MessageKind.ERROR


def test_unexpected_failure_restores_usable_state(qt_app: QApplication) -> None:
    assistant = FakeAssistant(error=RuntimeError("private technical detail"))
    window = make_window(assistant)
    window.command_input.setPlainText("open chrome")

    window.submit_command()
    wait_until(qt_app, lambda: not window.is_processing)

    assert assistant.calls == ["open chrome"]
    assert "private technical detail" not in window.conversation.messages[-1].text
    assert "Please try again" in window.conversation.messages[-1].text
    assert window.send_button.isEnabled()
    assert window.command_input.isEnabled()
    assert window.status_label.text() == "Ready"


def test_duplicate_submission_is_ignored_while_processing(qt_app: QApplication) -> None:
    assistant = BlockingAssistant()
    window = make_window(assistant)
    window.command_input.setPlainText("open notepad")

    window.submit_command()
    window.submit_command()
    wait_until(qt_app, lambda: len(assistant.calls) == 1)
    assert not window.send_button.isEnabled()

    assistant.release.set()
    wait_until(qt_app, lambda: not window.is_processing)

    assert assistant.calls == ["open notepad"]


def test_core_modules_do_not_import_qt() -> None:
    package_root = __import__("pathlib").Path(__file__).parents[1] / "src" / "desktop_assistant"
    core_modules = (
        "assistant.py",
        "router.py",
        "tools.py",
        "safety.py",
        "launcher.py",
        "models.py",
        "config.py",
        "bootstrap.py",
    )

    for filename in core_modules:
        assert "PySide6" not in (package_root / filename).read_text(encoding="utf-8")
