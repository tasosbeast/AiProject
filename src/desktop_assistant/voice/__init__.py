"""Voice input/output abstractions and provider adapters."""

from desktop_assistant.voice.models import AudioRecording, SpeechAudio
from desktop_assistant.voice.providers import (
    SpeechProvider,
    SpeechProviderError,
    TranscriptionProvider,
    TranscriptionProviderError,
)

__all__ = [
    "AudioRecording",
    "SpeechAudio",
    "SpeechProvider",
    "SpeechProviderError",
    "TranscriptionProvider",
    "TranscriptionProviderError",
]
