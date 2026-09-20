from __future__ import annotations

import os
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThreadPool, Signal, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from desktop_assistant.gui.hotkey import parse_hotkey
from desktop_assistant.gui.lifecycle import DesktopLifecycleController
from desktop_assistant.gui.main_window import MainWindow, OperationState
from desktop_assistant.models import AssistantResponse, ConfirmationRequest, RiskLevel, ToolResult


class FakeApplication(QObject):
    aboutToQuit = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.quit_on_last_window = True
        self.quit_calls = 0
        self._icon = QIcon()

    def setQuitOnLastWindowClosed(self, enabled: bool) -> None:
        self.quit_on_last_window = enabled

    def quit(self) -> None:
        self.quit_calls += 1

    def windowIcon(self) -> QIcon:
        return self._icon

    def setWindowIcon(self, icon: QIcon) -> None:
        self._icon = icon


class FakeAssistant:
    def __init__(self) -> None:
        self.handle_calls: list[str] = []
        self.shutdown_calls = 0

    def handle(self, request: str) -> AssistantResponse:
        self.handle_calls.append(request)
        return AssistantResponse.completed(ToolResult(True, "Done.", RiskLevel.SAFE))

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        raise AssertionError("confirmation execution is not expected")

    def cancel(self, confirmation_id: str) -> AssistantResponse:
        raise AssertionError("confirmation cancellation is not expected")

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class FakeRecorder(QObject):
    recording_ready = Signal(object)
    failed = Signal(str)
    maximum_seconds = 60
    is_available = True

    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def transcribe(self, recording: object) -> str:
        self.calls.append(recording)
        return "unused"


class FakeSpeechPlayer(QObject):
    finished = Signal()
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.stop_calls = 0

    def play(self, audio: object) -> None:
        pass

    def stop(self) -> None:
        self.stop_calls += 1


class FakeHotkey:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.register_calls = 0
        self.unregister_calls = 0
        self.callback: Callable[[], None] | None = None

    def register(self, callback: Callable[[], None]) -> bool:
        self.register_calls += 1
        self.callback = callback
        return self.succeeds

    def unregister(self) -> None:
        self.unregister_calls += 1

    def activate(self) -> None:
        assert self.callback is not None
        self.callback()


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_window(
    assistant: FakeAssistant,
    *,
    recorder: FakeRecorder | None = None,
    transcriber: FakeTranscriber | None = None,
    player: FakeSpeechPlayer | None = None,
) -> MainWindow:
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    return MainWindow(
        assistant,
        pool,
        recorder=recorder,
        transcription_provider=transcriber,
        speech_player=player,
    )


def test_default_and_configured_hotkeys_parse_to_expected_win32_values() -> None:
    default = parse_hotkey("Ctrl+Alt+Space")
    configured = parse_hotkey("win+shift+F12")

    assert default.display_text == "Ctrl+Alt+Space"
    assert default.modifiers == 0x0002 | 0x0001
    assert default.virtual_key == 0x20
    assert configured.display_text == "Shift+Win+F12"


@pytest.mark.parametrize(
    "value",
    ("", "Space", "Ctrl+Alt", "Ctrl+Ctrl+Space", "Ctrl+Alt+Delete", "Ctrl+A+B"),
)
def test_invalid_hotkey_configuration_fails_safely(value: str) -> None:
    with pytest.raises(ValueError):
        parse_hotkey(value)


def test_lifecycle_registers_hotkey_once_and_failure_does_not_crash(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    hotkey = FakeHotkey(succeeds=False)
    lifecycle = DesktopLifecycleController(
        FakeApplication(), window, tray_enabled=False, hotkey=hotkey, tray_available=False
    )

    lifecycle.start()
    lifecycle.start()

    assert hotkey.register_calls == 1
    window.command_input.setPlainText("still usable")
    assert window.command_input.toPlainText() == "still usable"
    window.perform_shutdown()


def test_hotkey_activation_shows_restores_and_focuses_without_side_effects(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    recorder = FakeRecorder()
    transcriber = FakeTranscriber()
    window = make_window(assistant, recorder=recorder, transcriber=transcriber)
    hotkey = FakeHotkey()
    lifecycle = DesktopLifecycleController(
        FakeApplication(), window, tray_enabled=False, hotkey=hotkey, tray_available=False
    )
    lifecycle.start()
    window.hide()

    hotkey.activate()
    qt_app.processEvents()

    assert window.isVisible()
    assert window.focusWidget() is window.command_input
    window.showMinimized()
    qt_app.processEvents()
    hotkey.activate()
    qt_app.processEvents()
    assert not window.isMinimized()
    assert window.isVisible()
    assert assistant.handle_calls == []
    assert recorder.start_calls == 0
    assert transcriber.calls == []
    lifecycle.request_quit()


def test_tray_menu_show_hide_and_x_preserve_pending_confirmation(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    application = FakeApplication()
    lifecycle = DesktopLifecycleController(
        application, window, tray_enabled=True, tray_available=True
    )
    lifecycle.start()
    assert lifecycle.tray_menu is not None
    actions = {action.text(): action for action in lifecycle.tray_menu.actions() if action.text()}
    assert set(actions) == {"Show Assistant", "Hide Assistant", "Quit"}
    request = ConfirmationRequest(
        "2395c26bcf4e4732b940616186313d71",
        "Close all open windows of:\nNotepad",
        RiskLevel.SENSITIVE,
        "The application may contain unsaved work.",
    )
    window._handle_result(AssistantResponse.confirmation_required(request))
    window.show()

    window.close()
    qt_app.processEvents()

    assert window.isHidden()
    assert assistant.shutdown_calls == 0
    assert window.operation_state is OperationState.AWAITING_CONFIRMATION
    assert window.conversation.confirmations[-1].isVisible() is False
    actions["Show Assistant"].trigger()
    qt_app.processEvents()
    assert window.isVisible()
    assert window.operation_state is OperationState.AWAITING_CONFIRMATION
    actions["Hide Assistant"].trigger()
    assert window.isHidden()
    lifecycle.request_quit()


def test_hiding_while_listening_cancels_recording(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    recorder = FakeRecorder()
    window = make_window(assistant, recorder=recorder, transcriber=FakeTranscriber())
    window.toggle_recording()
    assert window.operation_state is OperationState.LISTENING

    window.hide_to_tray()

    assert recorder.cancel_calls == 1
    assert window.operation_state is OperationState.READY
    assert assistant.handle_calls == []
    window.perform_shutdown()


@pytest.mark.parametrize(
    "state",
    (OperationState.TRANSCRIBING, OperationState.WORKING, OperationState.SPEAKING),
)
def test_hiding_does_not_duplicate_or_cancel_active_non_listening_work(
    qt_app: QApplication,
    state: OperationState,
) -> None:
    assistant = FakeAssistant()
    recorder = FakeRecorder()
    window = make_window(assistant, recorder=recorder, transcriber=FakeTranscriber())
    window._set_state(state)

    window.hide_to_tray()

    assert window.operation_state is state
    assert assistant.handle_calls == []
    assert recorder.cancel_calls == 0
    window.perform_shutdown()


def test_explicit_quit_unregisters_and_cleans_up_exactly_once(
    qt_app: QApplication,
) -> None:
    assistant = FakeAssistant()
    recorder = FakeRecorder()
    player = FakeSpeechPlayer()
    window = make_window(
        assistant,
        recorder=recorder,
        transcriber=FakeTranscriber(),
        player=player,
    )
    application = FakeApplication()
    hotkey = FakeHotkey()
    lifecycle = DesktopLifecycleController(
        application,
        window,
        tray_enabled=True,
        hotkey=hotkey,
        tray_available=True,
    )
    lifecycle.start()

    lifecycle.request_quit()
    lifecycle.request_quit()

    assert hotkey.unregister_calls == 1
    assert recorder.cancel_calls == 1
    assert player.stop_calls == 1
    assert assistant.shutdown_calls == 1
    assert application.quit_calls == 1


def test_no_tray_close_performs_normal_shutdown(qt_app: QApplication) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant)
    lifecycle = DesktopLifecycleController(
        FakeApplication(), window, tray_enabled=True, tray_available=False
    )
    lifecycle.start()
    window.show()

    window.close()
    qt_app.processEvents()

    assert assistant.shutdown_calls == 1
    assert window.isHidden()
