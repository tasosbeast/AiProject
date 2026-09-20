from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

from PySide6.QtCore import QThreadPool, Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop_assistant.gui.widgets import CommandInput, ConversationView, MessageKind
from desktop_assistant.gui.worker import AssistantWorker, BackgroundWorker
from desktop_assistant.models import AssistantResponse, AssistantResponseKind
from desktop_assistant.voice.models import AudioRecording, SpeechAudio
from desktop_assistant.voice.providers import SpeechProvider, TranscriptionProvider


logger = logging.getLogger(__name__)


class AssistantHandler(Protocol):
    def handle(self, request: str) -> AssistantResponse: ...

    def confirm(self, confirmation_id: str) -> AssistantResponse: ...

    def cancel(self, confirmation_id: str) -> AssistantResponse: ...

    def shutdown(self) -> None: ...


class VoiceRecorderHandler(Protocol):
    recording_ready: object
    failed: object
    maximum_seconds: int

    @property
    def is_available(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...


class SpeechPlayerHandler(Protocol):
    finished: object
    failed: object

    def play(self, audio: SpeechAudio) -> None: ...

    def stop(self) -> None: ...


class OperationState(str, Enum):
    READY = "Ready"
    LISTENING = "Listening..."
    TRANSCRIBING = "Transcribing..."
    WORKING = "Working..."
    SPEAKING = "Speaking..."
    AWAITING_CONFIRMATION = "Confirmation needed"


class MainWindow(QMainWindow):
    def __init__(
        self,
        assistant: AssistantHandler,
        thread_pool: QThreadPool | None = None,
        *,
        recorder: VoiceRecorderHandler | None = None,
        transcription_provider: TranscriptionProvider | None = None,
        speech_provider: SpeechProvider | None = None,
        speech_player: SpeechPlayerHandler | None = None,
        voice_output_enabled: bool = True,
    ) -> None:
        super().__init__()
        self._assistant = assistant
        self._thread_pool = thread_pool or QThreadPool(self)
        self._thread_pool.setMaxThreadCount(1)
        self._recorder = recorder
        self._transcription_provider = transcription_provider
        self._speech_provider = speech_provider
        self._speech_player = speech_player
        self._voice_output_enabled = voice_output_enabled
        self._active_worker: BackgroundWorker | None = None
        self._active_recording: AudioRecording | None = None
        self._state = OperationState.READY
        self._current_request_is_voice = False
        self._pending_confirmation_id: str | None = None
        self._hide_on_close = False
        self._force_close = False
        self._shutting_down = False
        self._shutdown_complete = False

        self.setWindowTitle("AI Assistant")
        self.resize(810, 620)
        self.setMinimumSize(620, 480)
        self.setCentralWidget(self._build_ui())

        self.command_input.submitted.connect(self.submit_command)
        self.send_button.clicked.connect(self.submit_command)
        self.mic_button.clicked.connect(self.toggle_recording)
        if self._recorder is not None:
            self._recorder.recording_ready.connect(self._handle_recording_ready)  # type: ignore[attr-defined]
            self._recorder.failed.connect(self._handle_recording_failure)  # type: ignore[attr-defined]
        if self._speech_player is not None:
            self._speech_player.finished.connect(self._handle_playback_finished)  # type: ignore[attr-defined]
            self._speech_player.failed.connect(self._handle_playback_failure)  # type: ignore[attr-defined]
        self._set_state(OperationState.READY)
        self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def is_processing(self) -> bool:
        return self._state is not OperationState.READY

    @property
    def operation_state(self) -> OperationState:
        return self._state

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def set_hide_on_close(self, enabled: bool) -> None:
        self._hide_on_close = enabled

    @Slot()
    def show_and_focus(self) -> None:
        if self._shutting_down:
            return
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        self.command_input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    @Slot()
    def hide_to_tray(self) -> None:
        if self._shutting_down:
            return
        if self._state is OperationState.LISTENING:
            try:
                if self._recorder is not None:
                    self._recorder.cancel()
            except Exception:
                logger.exception("Microphone could not be cancelled while hiding")
            self._cleanup_recording()
            self._current_request_is_voice = False
            self._set_state(OperationState.READY)
        self.hide()

    def prepare_for_quit(self) -> None:
        self._force_close = True

    def perform_shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutting_down = True
        self._shutdown_complete = True
        self._thread_pool.clear()
        if self._active_worker is not None:
            self._active_worker.cancel()
            self._active_worker = None
        self.send_button.setEnabled(False)
        self.command_input.setEnabled(False)
        self.mic_button.setEnabled(False)
        if self._recorder is not None:
            try:
                self._recorder.cancel()
            except Exception:
                logger.exception("Microphone could not be cancelled during shutdown")
        if self._speech_player is not None:
            try:
                self._speech_player.stop()
            except Exception:
                logger.exception("Speech playback could not be stopped during shutdown")
        self._cleanup_recording()
        self._pending_confirmation_id = None
        try:
            self._assistant.shutdown()
        except Exception:
            logger.exception("Pending confirmation could not be discarded during shutdown")

    def _build_ui(self) -> QWidget:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_header())

        self.conversation = ConversationView()
        root_layout.addWidget(self.conversation, 1)

        input_region = QWidget()
        input_layout = QVBoxLayout(input_region)
        input_layout.setContentsMargins(24, 8, 24, 22)
        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(11, 9, 9, 9)
        composer_layout.setSpacing(10)

        self.command_input = CommandInput()
        self.command_input.setObjectName("commandInput")
        self.command_input.setPlaceholderText("Type a command...")
        self.command_input.setMinimumHeight(44)
        self.command_input.setMaximumHeight(112)
        self.command_input.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.MinimumExpanding,
        )

        self.mic_button = QPushButton("Mic")
        self.mic_button.setObjectName("micButton")
        self.mic_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_button.setMinimumHeight(40)
        self.mic_button.setToolTip("Start push-to-talk recording")
        self.mic_button.setAccessibleName("Microphone")

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("sendButton")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.setMinimumHeight(40)

        composer_layout.addWidget(self.command_input, 1)
        composer_layout.addWidget(self.mic_button, 0, Qt.AlignmentFlag.AlignBottom)
        composer_layout.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        input_layout.addWidget(composer)
        root_layout.addWidget(input_region)
        return root

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(68)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(11)
        mark = QLabel("AI")
        mark.setObjectName("assistantMark")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("AI Assistant")
        title.setObjectName("assistantName")
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(8, 8)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusText")
        layout.addWidget(mark)
        layout.addWidget(title)
        layout.addStretch(1)
        layout.addWidget(self.status_dot)
        layout.addWidget(self.status_label)
        return header

    @Slot()
    def submit_command(self) -> None:
        if self._shutting_down or self._state is not OperationState.READY:
            return
        command = self.command_input.toPlainText().strip()
        if not command:
            self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.command_input.clear()
        self._start_assistant(command, is_voice=False)

    @Slot()
    def toggle_recording(self) -> None:
        if self._shutting_down:
            return
        if self._state is OperationState.LISTENING:
            self._set_state(OperationState.TRANSCRIBING)
            try:
                self._recorder.stop()  # type: ignore[union-attr]
            except Exception:
                logger.exception("Microphone could not stop")
                self._handle_recording_failure("Microphone recording failed.")
            return
        if self._state is not OperationState.READY:
            return
        if self._recorder is None or self._transcription_provider is None:
            self._voice_error("Voice input is not configured.")
            return
        try:
            self._recorder.start()
        except Exception:
            logger.exception("Microphone could not start")
            self._voice_error("The microphone is unavailable.")
            return
        self._set_state(OperationState.LISTENING)

    @Slot(object)
    def _handle_recording_ready(self, value: object) -> None:
        if self._shutting_down:
            if isinstance(value, AudioRecording):
                value.cleanup()
            return
        if not isinstance(value, AudioRecording):
            self._voice_error("The recording could not be processed.")
            return
        if self._state not in {OperationState.LISTENING, OperationState.TRANSCRIBING}:
            value.cleanup()
            return
        if self._active_worker is not None or self._active_recording is not None:
            value.cleanup()
            return
        self._set_state(OperationState.TRANSCRIBING)
        if not value.is_usable():
            value.cleanup()
            self._voice_error("No speech was recorded. Please try again.")
            return
        self._active_recording = value
        worker = BackgroundWorker(lambda: self._transcription_provider.transcribe(value))  # type: ignore[union-attr]
        worker.signals.succeeded.connect(self._handle_transcript)
        worker.signals.failed.connect(self._handle_transcription_failure)
        self._active_worker = worker
        self._thread_pool.start(worker)

    @Slot(str)
    def _handle_recording_failure(self, _message: str = "") -> None:
        if self._shutting_down:
            self._cleanup_recording()
            return
        self._cleanup_recording()
        self._voice_error("The microphone is unavailable or recording failed.")

    @Slot(object)
    def _handle_transcript(self, value: object) -> None:
        self._cleanup_recording()
        if self._shutting_down:
            return
        if self._state is not OperationState.TRANSCRIBING:
            return
        if not isinstance(value, str) or not value.strip():
            self._voice_error("No speech was recognized. Please try again.")
            return
        self._start_assistant(value.strip(), is_voice=True)

    @Slot()
    def _handle_transcription_failure(self) -> None:
        self._cleanup_recording()
        if self._shutting_down:
            return
        self._voice_error("Voice transcription is temporarily unavailable.")

    def _start_assistant(self, command: str, *, is_voice: bool) -> None:
        if self._shutting_down:
            return
        self.conversation.add_message(MessageKind.USER, command)
        self._current_request_is_voice = is_voice
        self._set_state(OperationState.WORKING)
        worker = AssistantWorker(self._assistant.handle, command)
        worker.signals.succeeded.connect(self._handle_result)
        worker.signals.failed.connect(self._handle_assistant_failure)
        self._active_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _handle_result(self, result: object) -> None:
        self._active_worker = None
        if self._shutting_down:
            return
        if not isinstance(result, AssistantResponse):
            logger.error("Assistant returned an unexpected result type: %s", type(result).__name__)
            self._handle_assistant_failure()
            return
        if result.kind is AssistantResponseKind.CONFIRMATION_REQUIRED:
            if result.confirmation is None:
                self._handle_assistant_failure()
                return
            self._pending_confirmation_id = result.confirmation.confirmation_id
            card = self.conversation.add_confirmation(result.confirmation)
            card.confirmed.connect(self._confirm_action)
            card.cancelled.connect(self._cancel_action)
            self._set_state(OperationState.AWAITING_CONFIRMATION)
            return
        if result.result is None:
            self._handle_assistant_failure()
            return
        tool_result = result.result
        kind = MessageKind.ASSISTANT if tool_result.success else MessageKind.ERROR
        self.conversation.add_message(kind, tool_result.message)
        if (
            self._current_request_is_voice
            and self._voice_output_enabled
            and self._speech_provider is not None
            and self._speech_player is not None
        ):
            self._set_state(OperationState.SPEAKING)
            worker = BackgroundWorker(lambda: self._speech_provider.synthesize(tool_result.message))
            worker.signals.succeeded.connect(self._handle_speech_audio)
            worker.signals.failed.connect(self._handle_speech_failure)
            self._active_worker = worker
            self._thread_pool.start(worker)
            return
        self._finish_processing()

    @Slot(str)
    def _confirm_action(self, confirmation_id: str) -> None:
        if (
            self._shutting_down
            or
            self._state is not OperationState.AWAITING_CONFIRMATION
            or confirmation_id != self._pending_confirmation_id
        ):
            return
        self._pending_confirmation_id = None
        self._set_state(OperationState.WORKING)
        worker = BackgroundWorker(lambda: self._assistant.confirm(confirmation_id))
        worker.signals.succeeded.connect(self._handle_result)
        worker.signals.failed.connect(self._handle_assistant_failure)
        self._active_worker = worker
        self._thread_pool.start(worker)

    @Slot(str)
    def _cancel_action(self, confirmation_id: str) -> None:
        if (
            self._shutting_down
            or
            self._state is not OperationState.AWAITING_CONFIRMATION
            or confirmation_id != self._pending_confirmation_id
        ):
            return
        self._pending_confirmation_id = None
        self._current_request_is_voice = False
        self._set_state(OperationState.WORKING)
        worker = BackgroundWorker(lambda: self._assistant.cancel(confirmation_id))
        worker.signals.succeeded.connect(self._handle_result)
        worker.signals.failed.connect(self._handle_assistant_failure)
        self._active_worker = worker
        self._thread_pool.start(worker)

    @Slot()
    def _handle_assistant_failure(self) -> None:
        if self._shutting_down:
            return
        self.conversation.add_message(
            MessageKind.ERROR,
            "Something went wrong while processing that command. Please try again.",
        )
        self._finish_processing()

    @Slot(object)
    def _handle_speech_audio(self, value: object) -> None:
        self._active_worker = None
        if self._shutting_down:
            return
        if not isinstance(value, SpeechAudio):
            self._handle_speech_failure()
            return
        try:
            self._speech_player.play(value)  # type: ignore[union-attr]
        except Exception:
            logger.exception("Generated speech could not be played")
            self._handle_speech_failure()

    @Slot()
    def _handle_speech_failure(self) -> None:
        if self._shutting_down:
            return
        self.conversation.add_message(MessageKind.ERROR, "Voice output is temporarily unavailable.")
        self._finish_processing()

    @Slot()
    def _handle_playback_finished(self) -> None:
        if self._shutting_down:
            return
        self._finish_processing()

    @Slot(str)
    def _handle_playback_failure(self, _message: str = "") -> None:
        if self._shutting_down:
            return
        self._handle_speech_failure()

    def _voice_error(self, message: str) -> None:
        if self._shutting_down:
            return
        self.conversation.add_message(MessageKind.ERROR, message)
        self._finish_processing()

    def _cleanup_recording(self) -> None:
        if self._active_recording is not None:
            self._active_recording.cleanup()
            self._active_recording = None

    def _finish_processing(self) -> None:
        self._active_worker = None
        if self._shutting_down:
            return
        self._pending_confirmation_id = None
        self._current_request_is_voice = False
        self._set_state(OperationState.READY)
        self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_state(self, state: OperationState) -> None:
        self._state = state
        ready = state is OperationState.READY
        listening = state is OperationState.LISTENING
        voice_configured = self._recorder is not None and self._transcription_provider is not None
        self.send_button.setEnabled(ready)
        self.command_input.setEnabled(ready)
        self.mic_button.setEnabled(listening or (ready and voice_configured))
        self.mic_button.setText("Stop" if listening else "Mic")
        self.mic_button.setToolTip(
            "Stop recording and transcribe" if listening else "Start push-to-talk recording"
        )
        self.mic_button.setProperty("recording", listening)
        self.mic_button.style().unpolish(self.mic_button)
        self.mic_button.style().polish(self.mic_button)
        self.status_label.setText(state.value)
        dot_state = "ready" if ready else "listening" if listening else "working"
        self.status_dot.setProperty("state", dot_state)
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._hide_on_close and not self._force_close:
            event.ignore()
            self.hide_to_tray()
            return
        self.perform_shutdown()
        super().closeEvent(event)
