from __future__ import annotations

from desktop_assistant.config import load_settings


def test_env_local_loads_when_process_environment_is_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("ASSISTANT_LOG_LEVEL", raising=False)
    monkeypatch.delenv("OPENAI_TRANSCRIBE_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TTS_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TTS_VOICE", raising=False)
    monkeypatch.delenv("VOICE_OUTPUT_ENABLED", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "OPENAI_API_KEY=test-local-key\n"
        "OPENAI_MODEL=test-local-model\n"
        "OPENAI_TRANSCRIBE_MODEL=local-transcribe\n"
        "OPENAI_TTS_MODEL=local-tts\n"
        "OPENAI_TTS_VOICE=cedar\n"
        "VOICE_OUTPUT_ENABLED=false\n"
        "ASSISTANT_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.openai_api_key == "test-local-key"
    assert settings.openai_model == "test-local-model"
    assert settings.log_level == "DEBUG"
    assert settings.openai_transcribe_model == "local-transcribe"
    assert settings.openai_tts_model == "local-tts"
    assert settings.openai_tts_voice == "cedar"
    assert not settings.voice_output_enabled


def test_process_environment_takes_precedence_over_env_local(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-process-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-process-model")
    monkeypatch.setenv("ASSISTANT_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("OPENAI_TRANSCRIBE_MODEL", "process-transcribe")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "process-tts")
    monkeypatch.setenv("OPENAI_TTS_VOICE", "cedar")
    monkeypatch.setenv("VOICE_OUTPUT_ENABLED", "false")
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "OPENAI_API_KEY=test-local-key\n"
        "OPENAI_MODEL=test-local-model\n"
        "OPENAI_TRANSCRIBE_MODEL=local-transcribe\n"
        "OPENAI_TTS_MODEL=local-tts\n"
        "OPENAI_TTS_VOICE=marin\n"
        "VOICE_OUTPUT_ENABLED=true\n"
        "ASSISTANT_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.openai_api_key == "test-process-key"
    assert settings.openai_model == "test-process-model"
    assert settings.log_level == "WARNING"
    assert settings.openai_transcribe_model == "process-transcribe"
    assert settings.openai_tts_model == "process-tts"
    assert settings.openai_tts_voice == "cedar"
    assert not settings.voice_output_enabled
