from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import desktop_assistant.voice.openai_audio as audio_module
from desktop_assistant.voice.models import AudioRecording
from desktop_assistant.voice.openai_audio import (
    TRANSCRIPTION_KEYWORDS,
    OpenAISpeechProvider,
    OpenAITranscriptionProvider,
)
from desktop_assistant.voice.providers import SpeechProviderError, TranscriptionProviderError


class FakeEndpoint:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAudioClient:
    def __init__(
        self,
        *,
        transcription: object = None,
        speech: object = None,
        error: Exception | None = None,
    ) -> None:
        self.audio = SimpleNamespace(
            transcriptions=FakeEndpoint(transcription, error),
            speech=FakeEndpoint(speech, error),
        )


def recording(tmp_path: Path) -> AudioRecording:
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF" + b"audio" * 200)
    return AudioRecording(path, 1_000)


def make_transcriber(client: FakeAudioClient) -> OpenAITranscriptionProvider:
    return OpenAITranscriptionProvider(
        api_key="test-key",
        model="test-transcribe",
        client=client,
    )


def test_transcription_preserves_greek_and_mixed_text_exactly(tmp_path: Path) -> None:
    greek = "Άνοιξε μου τον Chrome."
    client = FakeAudioClient(transcription=SimpleNamespace(text=f"  {greek}  "))

    result = make_transcriber(client).transcribe(recording(tmp_path))

    assert result == greek


def test_transcription_uses_greek_english_hints_and_controlled_keywords(tmp_path: Path) -> None:
    client = FakeAudioClient(transcription=SimpleNamespace(text="Άνοιξε το Spotify."))

    make_transcriber(client).transcribe(recording(tmp_path))

    request = client.audio.transcriptions.calls[0]
    assert request["model"] == "test-transcribe"
    assert request["languages"] == ["el", "en"]
    assert request["keywords"] == list(TRANSCRIPTION_KEYWORDS)
    assert "modern Greek" in str(request["prompt"])
    assert "translate" not in str(request["prompt"]).casefold()
    assert "language" not in request


@pytest.mark.parametrize(
    "exception_name",
    (
        "APITimeoutError",
        "AuthenticationError",
        "RateLimitError",
        "APIConnectionError",
        "APIStatusError",
        "APIError",
    ),
)
def test_transcription_api_failures_are_normalized(
    tmp_path: Path,
    monkeypatch,
    exception_name: str,
) -> None:
    class FakeSdkError(Exception):
        pass

    monkeypatch.setattr(audio_module, exception_name, FakeSdkError)
    client = FakeAudioClient(error=FakeSdkError("private provider detail"))

    with pytest.raises(TranscriptionProviderError):
        make_transcriber(client).transcribe(recording(tmp_path))


@pytest.mark.parametrize("response", (SimpleNamespace(text=""), SimpleNamespace(), object()))
def test_empty_or_malformed_transcription_is_rejected(tmp_path: Path, response: object) -> None:
    with pytest.raises(TranscriptionProviderError):
        make_transcriber(FakeAudioClient(transcription=response)).transcribe(recording(tmp_path))


def test_tts_uses_configured_model_voice_and_greek_instructions() -> None:
    response = SimpleNamespace(content=b"RIFF-audio")
    client = FakeAudioClient(speech=response)
    provider = OpenAISpeechProvider(
        api_key="test-key",
        model="test-tts",
        voice="cedar",
        client=client,
    )

    audio = provider.synthesize("Άνοιξε το Spotify.")

    assert audio.data == b"RIFF-audio"
    request = client.audio.speech.calls[0]
    assert request["input"] == "Άνοιξε το Spotify."
    assert request["model"] == "test-tts"
    assert request["voice"] == "cedar"
    assert request["response_format"] == "wav"
    assert "modern Greek" in str(request["instructions"])


def test_empty_tts_response_is_rejected() -> None:
    provider = OpenAISpeechProvider(
        api_key="test-key",
        model="test-tts",
        voice="marin",
        client=FakeAudioClient(speech=SimpleNamespace(content=b"")),
    )

    with pytest.raises(SpeechProviderError):
        provider.synthesize("Hello")
