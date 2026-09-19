from __future__ import annotations

from typing import Protocol

from desktop_assistant.voice.models import AudioRecording, SpeechAudio


class TranscriptionProviderError(RuntimeError):
    """A transcription request failed without exposing provider details."""


class SpeechProviderError(RuntimeError):
    """A speech-generation request failed without exposing provider details."""


class TranscriptionProvider(Protocol):
    def transcribe(self, recording: AudioRecording) -> str: ...


class SpeechProvider(Protocol):
    def synthesize(self, text: str) -> SpeechAudio: ...
