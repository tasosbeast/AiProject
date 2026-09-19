from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import (
    QAudioInput,
    QAudioOutput,
    QMediaCaptureSession,
    QMediaDevices,
    QMediaFormat,
    QMediaPlayer,
    QMediaRecorder,
)

from desktop_assistant.voice.models import AudioRecording, SpeechAudio


class VoiceCaptureError(RuntimeError):
    """The microphone could not start or finish a recording."""


class QtVoiceRecorder(QObject):
    """Explicit push-to-talk WAV recorder backed by Qt Multimedia."""

    recording_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, maximum_seconds: int = 60, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.maximum_seconds = maximum_seconds
        self._capture_session: QMediaCaptureSession | None = None
        self._audio_input: QAudioInput | None = None
        self._media_recorder: QMediaRecorder | None = None
        self._path: Path | None = None
        self._duration_ms = 0
        self._stopping = False
        self._limit_timer = QTimer(self)
        self._limit_timer.setSingleShot(True)
        self._limit_timer.timeout.connect(self.stop)

    @property
    def is_available(self) -> bool:
        return bool(QMediaDevices.audioInputs())

    @property
    def is_recording(self) -> bool:
        return self._media_recorder is not None and not self._stopping

    def start(self) -> None:
        if self.is_recording:
            raise VoiceCaptureError("A recording is already active.")
        if not self.is_available:
            raise VoiceCaptureError("No microphone is available.")

        self._cleanup_path()
        file_descriptor, filename = tempfile.mkstemp(prefix="ai-assistant-recording-", suffix=".wav")
        os.close(file_descriptor)
        Path(filename).unlink(missing_ok=True)
        self._path = Path(filename)
        self._duration_ms = 0
        self._stopping = False

        self._capture_session = QMediaCaptureSession(self)
        self._audio_input = QAudioInput(self)
        self._media_recorder = QMediaRecorder(self)

        media_format = QMediaFormat()
        media_format.setFileFormat(QMediaFormat.FileFormat.Wave)
        media_format.setAudioCodec(QMediaFormat.AudioCodec.Wave)
        self._media_recorder.setMediaFormat(media_format)
        self._media_recorder.setAudioSampleRate(44_100)
        self._media_recorder.setAudioChannelCount(1)
        self._media_recorder.setQuality(QMediaRecorder.Quality.HighQuality)
        self._media_recorder.setEncodingMode(QMediaRecorder.EncodingMode.ConstantQualityEncoding)
        self._media_recorder.setOutputLocation(QUrl.fromLocalFile(filename))
        self._media_recorder.durationChanged.connect(self._remember_duration)
        self._media_recorder.errorOccurred.connect(self._on_error)
        self._media_recorder.recorderStateChanged.connect(self._on_state_changed)
        self._capture_session.setAudioInput(self._audio_input)
        self._capture_session.setRecorder(self._media_recorder)
        self._media_recorder.record()
        if self._media_recorder.error() != QMediaRecorder.Error.NoError:
            message = self._media_recorder.errorString() or "The microphone could not start."
            self.cancel()
            raise VoiceCaptureError(message)
        self._limit_timer.start(self.maximum_seconds * 1000)

    def stop(self) -> None:
        if self._media_recorder is None or self._stopping:
            return
        self._stopping = True
        self._limit_timer.stop()
        self._media_recorder.stop()

    def cancel(self) -> None:
        self._limit_timer.stop()
        if self._media_recorder is not None:
            self._media_recorder.blockSignals(True)
            self._media_recorder.stop()
        self._release_media_objects()
        self._cleanup_path()
        self._stopping = False

    def _remember_duration(self, duration_ms: int) -> None:
        self._duration_ms = max(self._duration_ms, duration_ms)

    def _on_state_changed(self, state: QMediaRecorder.RecorderState) -> None:
        if not self._stopping or state != QMediaRecorder.RecorderState.StoppedState:
            return
        path = self._path
        duration_ms = self._duration_ms
        self._path = None
        self._stopping = False
        self._release_media_objects()
        if path is None:
            self.failed.emit("The recording could not be saved.")
            return
        self.recording_ready.emit(AudioRecording(path, duration_ms))

    def _on_error(self, _error: QMediaRecorder.Error, message: str) -> None:
        if _error == QMediaRecorder.Error.NoError:
            return
        self.cancel()
        self.failed.emit(message or "Microphone recording failed.")

    def _release_media_objects(self) -> None:
        for item in (self._media_recorder, self._audio_input, self._capture_session):
            if item is not None:
                item.deleteLater()
        self._capture_session = None
        self._audio_input = None
        self._media_recorder = None

    def _cleanup_path(self) -> None:
        if self._path is None:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
        self._path = None


class QtSpeechPlayer(QObject):
    """Plays generated speech from a short-lived local file and removes it."""

    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)
        self._path: Path | None = None

    def play(self, audio: SpeechAudio) -> None:
        self.stop()
        file_descriptor, filename = tempfile.mkstemp(
            prefix="ai-assistant-speech-",
            suffix=audio.suffix,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as output:
                output.write(audio.data)
        except Exception:
            Path(filename).unlink(missing_ok=True)
            raise
        self._path = Path(filename)
        self._player.setSource(QUrl.fromLocalFile(filename))
        self._player.play()

    def stop(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self._cleanup_path()

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop()
            self.finished.emit()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.stop()
            self.failed.emit("Generated speech could not be played.")

    def _on_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if _error == QMediaPlayer.Error.NoError:
            return
        self.stop()
        self.failed.emit(message or "Speech playback failed.")

    def _cleanup_path(self) -> None:
        if self._path is None:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
        self._path = None
