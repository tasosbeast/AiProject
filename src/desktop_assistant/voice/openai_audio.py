from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from desktop_assistant.voice.models import AudioRecording, SpeechAudio
from desktop_assistant.voice.providers import SpeechProviderError, TranscriptionProviderError


logger = logging.getLogger(__name__)

TRANSCRIPTION_LANGUAGES: tuple[str, ...] = ("el", "en")
TRANSCRIPTION_KEYWORDS: tuple[str, ...] = (
    "Spotify",
    "Chrome",
    "Google Chrome",
    "VS Code",
    "Visual Studio Code",
    "Notepad",
    "File Explorer",
    "GitHub",
    "Downloads",
    "Documents",
    "Desktop",
    "Windows",
    "OpenAI",
    "AI Assistant",
)
TRANSCRIPTION_CONTEXT = (
    "The speaker primarily speaks modern Greek and may naturally code-switch into English "
    "for Windows applications, websites, technical terminology, filenames, and product names. "
    "Preserve the spoken language and natural Greek-English code-switching faithfully."
)
TTS_INSTRUCTIONS = (
    "Speak in natural modern Greek when the text is Greek, clearly and conversationally, "
    "with calm pacing. Pronounce English application names and technical terms naturally "
    "rather than translating them. Preserve the language of the input text."
)


class OpenAITranscriptionProvider:
    """High-accuracy, stateless transcription for completed recordings."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 1,
        client: object | None = None,
    ) -> None:
        self._model = model
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def transcribe(self, recording: AudioRecording) -> str:
        started = perf_counter()
        try:
            with recording.path.open("rb") as audio_file:
                response = self._client.audio.transcriptions.create(  # type: ignore[attr-defined]
                    model=self._model,
                    file=audio_file,
                    prompt=TRANSCRIPTION_CONTEXT,
                    languages=list(TRANSCRIPTION_LANGUAGES),
                    keywords=list(TRANSCRIPTION_KEYWORDS),
                    response_format="json",
                )
        except (
            APITimeoutError,
            AuthenticationError,
            RateLimitError,
            APIConnectionError,
            APIStatusError,
            APIError,
        ) as exc:
            self._log_failure(type(exc).__name__, started)
            raise TranscriptionProviderError("Transcription service is unavailable.") from exc
        except OSError as exc:
            raise TranscriptionProviderError("The recording could not be read.") from exc

        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise TranscriptionProviderError("Transcription response was malformed.")
        transcript = text.strip()
        if not transcript:
            raise TranscriptionProviderError("No speech was recognized.")
        logger.info(
            "Audio transcribed",
            extra={
                "model": self._model,
                "latency_ms": round((perf_counter() - started) * 1000),
                "transcript_length": len(transcript),
            },
        )
        return transcript

    def _log_failure(self, category: str, started: float) -> None:
        logger.warning(
            "Transcription provider failed",
            extra={
                "model": self._model,
                "failure_category": category,
                "latency_ms": round((perf_counter() - started) * 1000),
            },
        )


class OpenAISpeechProvider:
    """Creates one temporary in-memory WAV response for an assistant message."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 1,
        client: object | None = None,
    ) -> None:
        self._model = model
        self._voice = voice
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def synthesize(self, text: str) -> SpeechAudio:
        started = perf_counter()
        try:
            response = self._client.audio.speech.create(  # type: ignore[attr-defined]
                model=self._model,
                voice=self._voice,
                input=text,
                instructions=TTS_INSTRUCTIONS,
                response_format="wav",
            )
            data = self._read_audio(response)
        except (
            APITimeoutError,
            AuthenticationError,
            RateLimitError,
            APIConnectionError,
            APIStatusError,
            APIError,
        ) as exc:
            self._log_failure(type(exc).__name__, started)
            raise SpeechProviderError("Speech service is unavailable.") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise SpeechProviderError("Speech response was malformed.") from exc

        if not data:
            raise SpeechProviderError("Speech response was empty.")
        logger.info(
            "Speech generated",
            extra={
                "model": self._model,
                "voice": self._voice,
                "latency_ms": round((perf_counter() - started) * 1000),
                "audio_bytes": len(data),
            },
        )
        return SpeechAudio(data=data)

    @staticmethod
    def _read_audio(response: Any) -> bytes:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            return content
        reader = getattr(response, "read", None)
        if callable(reader):
            value = reader()
            if isinstance(value, bytes):
                return value
        raise TypeError("Speech response did not contain audio bytes.")

    def _log_failure(self, category: str, started: float) -> None:
        logger.warning(
            "Speech provider failed",
            extra={
                "model": self._model,
                "voice": self._voice,
                "failure_category": category,
                "latency_ms": round((perf_counter() - started) * 1000),
            },
        )
