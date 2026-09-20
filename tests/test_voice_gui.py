from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtWidgets import QApplication

from desktop_assistant.gui.audio import QtSpeechPlayer, QtVoiceRecorder
from desktop_assistant.gui.main_window import MainWindow, OperationState
from desktop_assistant.models import AssistantResponse, ConfirmationRequest, RiskLevel, ToolResult
from desktop_assistant.voice.models import AudioRecording, SpeechAudio


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeAssistant:
    def __init__(self, message: str = "Opening Spotify.") -> None:
        self.calls: list[str] = []
        self.message = message

    def handle(self, request: str) -> AssistantResponse:
        self.calls.append(request)
        return AssistantResponse.completed(ToolResult(True, self.message, RiskLevel.SAFE))

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        raise AssertionError("No confirmation expected")

    def cancel(self, confirmation_id: str) -> AssistantResponse:
        raise AssertionError("No confirmation expected")

    def shutdown(self) -> None:
        pass


class ConfirmingVoiceAssistant(FakeAssistant):
    def __init__(self) -> None:
        super().__init__()
        self.confirm_calls: list[str] = []
        self.request = ConfirmationRequest(
            "6f0690769ab6470eb4f1091405b829e6",
            "Change exactly: A -> B",
            RiskLevel.SENSITIVE,
            "Review this exact action.",
        )

    def handle(self, request: str) -> AssistantResponse:
        self.calls.append(request)
        return AssistantResponse.confirmation_required(self.request)

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        self.confirm_calls.append(confirmation_id)
        return AssistantResponse.completed(
            ToolResult(True, "Confirmed action completed.", RiskLevel.SENSITIVE)
        )


class FakeRecorder(QObject):
    recording_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, *, available: bool = True, start_error: bool = False) -> None:
        super().__init__()
        self.maximum_seconds = 60
        self.is_available = available
        self.start_error = start_error
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.start_error or not self.is_available:
            raise RuntimeError("microphone unavailable")

    def stop(self) -> None:
        self.stop_calls += 1

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakeTranscriber:
    def __init__(self, transcript: str = "Άνοιξε το Spotify.", error: Exception | None = None) -> None:
        self.transcript = transcript
        self.error = error
        self.calls: list[AudioRecording] = []

    def transcribe(self, value: AudioRecording) -> str:
        self.calls.append(value)
        if self.error is not None:
            raise self.error
        return self.transcript


class BlockingTranscriber:
    def __init__(self, transcript: str = "Άνοιξε το Spotify.") -> None:
        self.transcript = transcript
        self.started = Event()
        self.release = Event()
        self.calls: list[AudioRecording] = []

    def transcribe(self, value: AudioRecording) -> str:
        self.calls.append(value)
        self.started.set()
        self.release.wait(timeout=2.0)
        return self.transcript


class FakeSpeechProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def synthesize(self, text: str) -> SpeechAudio:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return SpeechAudio(b"RIFF-audio")


class FakeSpeechPlayer(QObject):
    finished = Signal()
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.played: list[SpeechAudio] = []
        self.stop_calls = 0

    def play(self, audio: SpeechAudio) -> None:
        self.played.append(audio)

    def stop(self) -> None:
        self.stop_calls += 1


def wait_until(app: QApplication, predicate: object, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for GUI state")


def make_recording(path: Path, *, usable: bool = True) -> AudioRecording:
    path.write_bytes(b"x" * (1_024 if usable else 4))
    return AudioRecording(path, 1_000 if usable else 0)


def make_window(
    assistant: FakeAssistant,
    recorder: FakeRecorder,
    transcriber: FakeTranscriber,
    *,
    speech: FakeSpeechProvider | None = None,
    player: FakeSpeechPlayer | None = None,
    voice_output_enabled: bool = False,
) -> MainWindow:
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    return MainWindow(
        assistant,
        pool,
        recorder=recorder,
        transcription_provider=transcriber,
        speech_provider=speech,
        speech_player=player,
        voice_output_enabled=voice_output_enabled,
    )


def test_microphone_click_starts_and_second_click_stops(qt_app: QApplication) -> None:
    recorder = FakeRecorder()
    window = make_window(FakeAssistant(), recorder, FakeTranscriber())

    window.toggle_recording()

    assert recorder.start_calls == 1
    assert window.operation_state is OperationState.LISTENING
    assert window.status_label.text() == "Listening..."
    assert window.mic_button.property("recording") is True

    window.toggle_recording()

    assert recorder.stop_calls == 1
    assert window.operation_state is OperationState.TRANSCRIBING
    assert window.status_label.text() == "Transcribing..."


def test_microphone_failure_returns_to_ready(qt_app: QApplication) -> None:
    recorder = FakeRecorder(available=False)
    assistant = FakeAssistant()
    window = make_window(assistant, recorder, FakeTranscriber())

    window.toggle_recording()

    assert window.operation_state is OperationState.READY
    assert assistant.calls == []
    assert "microphone" in window.conversation.messages[-1].text.casefold()


def test_empty_recording_skips_transcription_and_is_deleted(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    transcriber = FakeTranscriber()
    window = make_window(FakeAssistant(), recorder, transcriber)
    recording = make_recording(tmp_path / "empty.wav", usable=False)
    window.toggle_recording()
    window.toggle_recording()

    recorder.recording_ready.emit(recording)
    qt_app.processEvents()

    assert transcriber.calls == []
    assert not recording.path.exists()
    assert window.operation_state is OperationState.READY


@pytest.mark.parametrize(
    "transcript",
    (
        "Άνοιξε το Spotify.",
        "Άνοιξε μου τον Chrome και μετά άνοιξε το VS Code.",
    ),
)
def test_transcript_is_preserved_displayed_and_sent_once(
    qt_app: QApplication,
    tmp_path: Path,
    transcript: str,
) -> None:
    recorder = FakeRecorder()
    transcriber = FakeTranscriber(transcript)
    assistant = FakeAssistant()
    window = make_window(assistant, recorder, transcriber)
    recording = make_recording(tmp_path / "voice.wav")
    window.toggle_recording()
    window.toggle_recording()

    recorder.recording_ready.emit(recording)
    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    assert assistant.calls == [transcript]
    assert window.conversation.messages[0].text == transcript
    assert not recording.path.exists()


def test_transcription_failure_executes_nothing_and_cleans_recording(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    assistant = FakeAssistant()
    transcriber = FakeTranscriber(error=TimeoutError("private detail"))
    window = make_window(assistant, recorder, transcriber)
    recording = make_recording(tmp_path / "voice.wav")
    window.toggle_recording()
    window.toggle_recording()

    recorder.recording_ready.emit(recording)
    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    assert assistant.calls == []
    assert not recording.path.exists()
    assert "private detail" not in window.conversation.messages[-1].text


def test_tts_receives_assistant_response_not_transcript(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    assistant = FakeAssistant("Opening Chrome.")
    speech = FakeSpeechProvider()
    player = FakeSpeechPlayer()
    window = make_window(
        assistant,
        recorder,
        FakeTranscriber("Άνοιξε τον Chrome."),
        speech=speech,
        player=player,
        voice_output_enabled=True,
    )
    window.toggle_recording()
    window.toggle_recording()
    recorder.recording_ready.emit(make_recording(tmp_path / "voice.wav"))

    wait_until(qt_app, lambda: bool(player.played))

    assert speech.calls == ["Opening Chrome."]
    assert speech.calls != assistant.calls
    assert window.operation_state is OperationState.SPEAKING
    player.finished.emit()
    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)


def test_tts_failure_preserves_result_and_recovers(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    assistant = FakeAssistant("Opening Notepad.")
    speech = FakeSpeechProvider(error=RuntimeError("private tts detail"))
    player = FakeSpeechPlayer()
    window = make_window(
        assistant,
        recorder,
        FakeTranscriber("Άνοιξε το Notepad."),
        speech=speech,
        player=player,
        voice_output_enabled=True,
    )
    window.toggle_recording()
    window.toggle_recording()
    recorder.recording_ready.emit(make_recording(tmp_path / "voice.wav"))

    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    texts = [message.text for message in window.conversation.messages]
    assert "Opening Notepad." in texts
    assert "private tts detail" not in texts[-1]


def test_disabled_voice_output_skips_tts(qt_app: QApplication, tmp_path: Path) -> None:
    recorder = FakeRecorder()
    speech = FakeSpeechProvider()
    window = make_window(
        FakeAssistant(),
        recorder,
        FakeTranscriber(),
        speech=speech,
        player=FakeSpeechPlayer(),
        voice_output_enabled=False,
    )
    window.toggle_recording()
    window.toggle_recording()
    recorder.recording_ready.emit(make_recording(tmp_path / "voice.wav"))

    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    assert speech.calls == []


def test_duplicate_recording_result_is_ignored(qt_app: QApplication, tmp_path: Path) -> None:
    recorder = FakeRecorder()
    transcriber = FakeTranscriber()
    assistant = FakeAssistant()
    window = make_window(assistant, recorder, transcriber)
    first = make_recording(tmp_path / "first.wav")
    second = make_recording(tmp_path / "second.wav")
    window.toggle_recording()
    window.toggle_recording()

    recorder.recording_ready.emit(first)
    recorder.recording_ready.emit(second)
    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    assert len(transcriber.calls) == 1
    assert len(assistant.calls) == 1
    assert not second.path.exists()


def test_voice_confirmation_does_not_retranscribe_or_accept_spoken_yes(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    transcriber = FakeTranscriber("Κάνε τη δοκιμαστική ενέργεια.")
    assistant = ConfirmingVoiceAssistant()
    window = make_window(assistant, recorder, transcriber)
    window.toggle_recording()
    window.toggle_recording()
    recorder.recording_ready.emit(make_recording(tmp_path / "confirmation.wav"))
    wait_until(qt_app, lambda: bool(window.conversation.confirmations))

    window._handle_transcript("ναι")
    qt_app.processEvents()
    assert assistant.confirm_calls == []
    assert assistant.calls == ["Κάνε τη δοκιμαστική ενέργεια."]

    window.conversation.confirmations[-1].confirm_button.click()
    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    assert len(transcriber.calls) == 1
    assert assistant.calls == ["Κάνε τη δοκιμαστική ενέργεια."]
    assert assistant.confirm_calls == [assistant.request.confirmation_id]


def test_typed_command_still_uses_original_path(qt_app: QApplication) -> None:
    assistant = FakeAssistant()
    window = make_window(assistant, FakeRecorder(), FakeTranscriber())
    window.command_input.setPlainText("open spotify")

    window.submit_command()
    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    assert assistant.calls == ["open spotify"]


def test_recorder_exposes_bounded_duration() -> None:
    recorder = QtVoiceRecorder(60)
    assert recorder.maximum_seconds == 60


def test_speech_player_removes_temporary_audio_on_stop(qt_app: QApplication) -> None:
    player = QtSpeechPlayer()
    player.play(SpeechAudio(b"RIFF" + b"audio" * 200))
    path = player._path

    assert path is not None and path.exists()
    player.stop()
    assert not path.exists()


def test_voice_provider_modules_cannot_execute_tools() -> None:
    root = Path(__file__).parents[1] / "src" / "desktop_assistant" / "voice"
    forbidden = ("tool_registry", "launcher", "subprocess", "powershell", "cmd.exe")

    for source_path in root.glob("*.py"):
        source = source_path.read_text(encoding="utf-8").casefold()
        assert all(term not in source for term in forbidden)


def test_late_recording_callback_after_shutdown_only_cleans_audio(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    transcriber = FakeTranscriber()
    assistant = FakeAssistant()
    window = make_window(assistant, recorder, transcriber)
    recording = make_recording(tmp_path / "late.wav")
    window.perform_shutdown()

    recorder.recording_ready.emit(recording)
    qt_app.processEvents()

    assert not recording.path.exists()
    assert transcriber.calls == []
    assert assistant.calls == []
    assert window.is_shutting_down


def test_quit_during_transcription_ignores_late_transcript(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    transcriber = BlockingTranscriber()
    assistant = FakeAssistant()
    window = make_window(assistant, recorder, transcriber)  # type: ignore[arg-type]
    recording = make_recording(tmp_path / "voice.wav")
    window.toggle_recording()
    recorder.recording_ready.emit(recording)
    wait_until(qt_app, lambda: len(transcriber.calls) == 1)

    assert window.operation_state is OperationState.TRANSCRIBING

    window.perform_shutdown()
    transcriber.release.set()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)

    assert assistant.calls == []
    assert window.is_shutting_down


def test_quit_during_transcription_ignores_late_failure(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    transcriber = BlockingTranscriber()
    assistant = FakeAssistant()
    window = make_window(assistant, recorder, transcriber)  # type: ignore[arg-type]
    recording = make_recording(tmp_path / "voice.wav")
    window.toggle_recording()
    recorder.recording_ready.emit(recording)
    wait_until(qt_app, lambda: len(transcriber.calls) == 1)

    assert window.operation_state is OperationState.TRANSCRIBING
    original_messages = list(window.conversation.messages)

    window.perform_shutdown()
    window._handle_transcription_failure()
    transcriber.release.set()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)

    assert window.conversation.messages == original_messages


def test_quit_during_tts_generation_ignores_late_speech_audio_and_failure(
    qt_app: QApplication,
) -> None:
    speech_provider = FakeSpeechProvider()
    speech_player = FakeSpeechPlayer()
    assistant = FakeAssistant()
    window = make_window(
        assistant,
        FakeRecorder(),
        FakeTranscriber(),
        speech=speech_provider,
        player=speech_player,
        voice_output_enabled=True,
    )
    window.perform_shutdown()
    original_messages = list(window.conversation.messages)

    window._handle_speech_audio(SpeechAudio(b"RIFF" + b"late" * 20))
    window._handle_speech_failure()
    window._handle_playback_finished()
    window._handle_playback_failure("error")

    assert speech_player.played == []
    assert window.conversation.messages == original_messages


def test_voice_multi_step_plan_executes_without_repeated_routing(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    class PlanAssistant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def handle(self, request: str, cancellation_token: object = None) -> AssistantResponse:
            self.calls.append(request)
            return AssistantResponse.completed(
                ToolResult(
                    True,
                    "Completed 2 actions:\n1. Opening Spotify.\n2. Opening Chrome.",
                    RiskLevel.SAFE,
                )
            )

        def confirm(self, confirmation_id: str) -> AssistantResponse:
            raise AssertionError("No confirmation expected")

        def cancel(self, confirmation_id: str) -> AssistantResponse:
            raise AssertionError("No confirmation expected")

        def shutdown(self) -> None:
            pass

    assistant = PlanAssistant()
    recorder = FakeRecorder()
    transcriber = FakeTranscriber("Άνοιξε το Spotify και το Chrome.")
    window = make_window(assistant, recorder, transcriber)  # type: ignore[arg-type]

    window.toggle_recording()
    window.toggle_recording()
    recorder.recording_ready.emit(make_recording(tmp_path / "voice.wav"))

    wait_until(qt_app, lambda: window.operation_state is OperationState.READY)

    # Exactly one handle call for the entire multi-step request
    assert assistant.calls == ["Άνοιξε το Spotify και το Chrome."]
    assert len(transcriber.calls) == 1
    assert "Completed 2 actions:" in window.conversation.messages[-1].text


def test_gui_quit_during_confirmed_plan_sensitive_action_allows_sensitive_and_stops_continuation(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    req = ConfirmationRequest(
        "conf-12345",
        "Close Spotify",
        RiskLevel.SENSITIVE,
        "Warning message",
    )

    class ConfirmingAssistant:
        def __init__(self) -> None:
            self.handle_calls: list[str] = []
            self.confirm_entered = Event()
            self.confirm_release = Event()
            self.sensitive_completed = False
            self.shutdown_called = False
            self.continuation_stopped = False

        def handle(self, request: str, cancellation_token: object = None) -> AssistantResponse:
            self.handle_calls.append(request)
            return AssistantResponse.confirmation_required(req)

        def confirm(self, confirmation_id: str) -> AssistantResponse:
            self.confirm_entered.set()
            self.confirm_release.wait(timeout=3.0)
            self.sensitive_completed = True
            if self.shutdown_called:
                self.continuation_stopped = True
                return AssistantResponse.completed(
                    ToolResult(
                        False,
                        "Execution cancelled at step 3 of 3. 2 action(s) completed before cancellation.",
                        RiskLevel.SENSITIVE,
                    )
                )
            return AssistantResponse.completed(
                ToolResult(True, "All actions completed.", RiskLevel.SENSITIVE)
            )

        def cancel(self, confirmation_id: str) -> AssistantResponse:
            raise AssertionError("Cancel not expected")

        def shutdown(self) -> None:
            self.shutdown_called = True

    assistant = ConfirmingAssistant()
    window = make_window(assistant, FakeRecorder(), FakeTranscriber())  # type: ignore[arg-type]

    window.command_input.setPlainText("Close Spotify then open Chrome")
    window.submit_command()

    wait_until(qt_app, lambda: bool(window.conversation.confirmations))

    # Click confirm
    window.conversation.confirmations[-1].confirm_button.click()
    assert assistant.confirm_entered.wait(timeout=2.0)

    # Perform quit/shutdown while confirmed sensitive action is actively running
    window.perform_shutdown()
    assert assistant.shutdown_called is True

    # Release sensitive action to finish
    assistant.confirm_release.set()

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)

    assert assistant.sensitive_completed is True
    assert assistant.continuation_stopped is True
    assert window.is_shutting_down


