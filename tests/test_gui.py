from __future__ import annotations

import os
import time
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QLabel

from desktop_assistant.gui.main_window import MainWindow, OperationState
from desktop_assistant.gui.widgets import MessageKind
from desktop_assistant.models import (
    AssistantResponse,
    ConfirmationRequest,
    RiskLevel,
    ToolResult,
)


class FakeAssistant:
    def __init__(self, result: ToolResult | None = None, error: Exception | None = None) -> None:
        self.result = result or ToolResult(True, "Opening Spotify.", RiskLevel.SAFE)
        self.error = error
        self.calls: list[str] = []

    def handle(self, request: str) -> AssistantResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return AssistantResponse.completed(self.result)

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        raise AssertionError("No confirmation expected")

    def cancel(self, confirmation_id: str) -> AssistantResponse:
        raise AssertionError("No confirmation expected")

    def shutdown(self) -> None:
        pass


class BlockingAssistant(FakeAssistant):
    def __init__(self) -> None:
        super().__init__()
        self.release = Event()

    def handle(self, request: str) -> AssistantResponse:
        self.calls.append(request)
        self.release.wait(timeout=2)
        return AssistantResponse.completed(self.result)


class ConfirmingAssistant(FakeAssistant):
    def __init__(self, risk: RiskLevel = RiskLevel.SENSITIVE) -> None:
        super().__init__()
        self.confirm_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.request = ConfirmationRequest(
            "4b39dbdf8a81479d8d08c026d5f9406f",
            "Change exactly: A -> B",
            risk,
            "Review this exact action.",
        )

    def handle(self, request: str) -> AssistantResponse:
        self.calls.append(request)
        return AssistantResponse.confirmation_required(self.request)

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        self.confirm_calls.append(confirmation_id)
        return AssistantResponse.completed(
            ToolResult(True, "Confirmed action completed.", self.request.risk_level)
        )

    def cancel(self, confirmation_id: str) -> AssistantResponse:
        self.cancel_calls.append(confirmation_id)
        return AssistantResponse.completed(ToolResult(True, "Action cancelled.", RiskLevel.SAFE))


class FakeThreadPool:
    def __init__(self) -> None:
        self.clear_calls = 0

    def setMaxThreadCount(self, _count: int) -> None:
        pass

    def start(self, _worker: object) -> None:
        pass

    def clear(self) -> None:
        self.clear_calls += 1


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


def test_provider_failure_result_returns_gui_to_ready(qt_app: QApplication) -> None:
    result = ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
    assistant = FakeAssistant(result=result)
    window = make_window(assistant)
    window.command_input.setPlainText("Could you open Chrome?")

    window.submit_command()
    wait_until(qt_app, lambda: not window.is_processing)

    assert assistant.calls == ["Could you open Chrome?"]
    assert window.conversation.messages[-1].text == result.message
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


def test_confirmation_card_disables_input_and_calls_confirm_directly(
    qt_app: QApplication,
) -> None:
    assistant = ConfirmingAssistant()
    window = make_window(assistant)
    window.command_input.setPlainText("natural sensitive request")

    window.submit_command()
    wait_until(qt_app, lambda: bool(window.conversation.confirmations))

    card = window.conversation.confirmations[-1]
    assert card.request.summary == "Change exactly: A -> B"
    assert card.request.risk_level is RiskLevel.SENSITIVE
    assert card.findChild(QLabel, "confirmationRisk").text() == "Risk: Sensitive"
    assert not window.command_input.isEnabled()
    assert not window.send_button.isEnabled()
    assert not window.mic_button.isEnabled()
    card.confirm_button.click()
    wait_until(qt_app, lambda: not window.is_processing)

    assert assistant.calls == ["natural sensitive request"]
    assert assistant.confirm_calls == [assistant.request.confirmation_id]
    assert assistant.cancel_calls == []
    assert window.conversation.messages[-1].text == "Confirmed action completed."
    assert window.status_label.text() == "Ready"


def test_confirmation_cancel_calls_direct_api_and_returns_ready(qt_app: QApplication) -> None:
    assistant = ConfirmingAssistant(RiskLevel.DESTRUCTIVE)
    window = make_window(assistant)
    window.command_input.setPlainText("natural destructive request")
    window.submit_command()
    wait_until(qt_app, lambda: bool(window.conversation.confirmations))

    window.conversation.confirmations[-1].cancel_button.click()
    wait_until(qt_app, lambda: not window.is_processing)

    assert assistant.calls == ["natural destructive request"]
    assert assistant.confirm_calls == []
    assert assistant.cancel_calls == [assistant.request.confirmation_id]
    assert window.conversation.messages[-1].text == "Action cancelled."
    assert window.command_input.isEnabled()


def test_confirmation_card_wraps_multiline_filesystem_summary(qt_app: QApplication) -> None:
    assistant = ConfirmingAssistant()
    assistant.request = ConfirmationRequest(
        assistant.request.confirmation_id,
        "Rename:\nC:\\Users\\tasos\\Downloads\\draft.txt\n→\n"
        "C:\\Users\\tasos\\Downloads\\final.txt",
        RiskLevel.SENSITIVE,
        assistant.request.warning,
    )
    window = make_window(assistant)
    window.command_input.setPlainText("rename request")

    window.submit_command()
    wait_until(qt_app, lambda: bool(window.conversation.confirmations))
    summary = window.conversation.confirmations[-1].findChild(QLabel, "confirmationSummary")

    assert summary.wordWrap()
    assert "draft.txt\n→\n" in summary.text()


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
        "tool_registry.py",
        "known_folders.py",
        "filesystem.py",
        "filesystem_tools.py",
    )

    for filename in core_modules:
        assert "PySide6" not in (package_root / filename).read_text(encoding="utf-8")


def test_gui_modules_do_not_import_openai() -> None:
    gui_root = __import__("pathlib").Path(__file__).parents[1] / "src" / "desktop_assistant" / "gui"

    for path in gui_root.glob("*.py"):
        assert "openai" not in path.read_text(encoding="utf-8").casefold()


def test_cli_does_not_import_voice_or_qt() -> None:
    package_root = __import__("pathlib").Path(__file__).parents[1] / "src" / "desktop_assistant"
    cli_source = (package_root / "cli.py").read_text(encoding="utf-8").casefold()

    assert "voice" not in cli_source
    assert "pyside6" not in cli_source


def test_shutdown_rejects_new_work_and_ignores_late_result(qt_app: QApplication) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    window.perform_shutdown()
    original_messages = list(window.conversation.messages)

    window.command_input.setPlainText("open spotify")
    window.submit_command()
    window._handle_result(
        AssistantResponse.completed(ToolResult(True, "Late result.", RiskLevel.SAFE))
    )

    assert window.is_shutting_down
    assert assistant.calls == []
    assert window.conversation.messages == original_messages
    assert not window.command_input.isEnabled()
    assert not window.send_button.isEnabled()
    assert not window.mic_button.isEnabled()


def test_active_worker_completion_after_shutdown_cannot_mutate_ui(
    qt_app: QApplication,
) -> None:
    assistant = BlockingAssistant()
    window = make_window(assistant)
    window.command_input.setPlainText("open notepad")
    window.submit_command()
    wait_until(qt_app, lambda: len(assistant.calls) == 1)
    messages_before_shutdown = list(window.conversation.messages)

    window.perform_shutdown()
    assistant.release.set()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)

    assert window.conversation.messages == messages_before_shutdown
    assert window.is_shutting_down
    assert not window.command_input.isEnabled()


def test_shutdown_clears_queued_background_work(qt_app: QApplication) -> None:
    pool = FakeThreadPool()
    window = MainWindow(FakeAssistant(), pool)  # type: ignore[arg-type]

    window.perform_shutdown()
    window.perform_shutdown()

    assert pool.clear_calls == 1


def test_late_confirmation_after_shutdown_cannot_create_confirmation_card(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    window.perform_shutdown()
    original_messages = list(window.conversation.messages)

    window._handle_result(
        AssistantResponse.confirmation_required(
            ConfirmationRequest("cid-1", "Delete world", RiskLevel.DESTRUCTIVE, "Be careful")
        )
    )

    assert window.conversation.messages == original_messages
    assert window._pending_confirmation_id is None
    assert window.conversation.confirmations == []


def test_late_assistant_failure_after_shutdown_is_ignored(qt_app: QApplication) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    window.perform_shutdown()
    original_messages = list(window.conversation.messages)

    window._handle_assistant_failure()

    assert window.conversation.messages == original_messages


def test_late_confirm_or_cancel_action_after_shutdown_is_ignored(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    window._pending_confirmation_id = "test-id"
    window._set_state(OperationState.AWAITING_CONFIRMATION)
    window.perform_shutdown()

    window._confirm_action("test-id")
    window._cancel_action("test-id")

    assert assistant.calls == []

