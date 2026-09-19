from __future__ import annotations

from desktop_assistant.bootstrap import build_assistant, build_voice_services
from desktop_assistant.config import Settings

from conftest import FakeLauncher


def test_missing_api_key_does_not_break_bootstrap_or_deterministic_commands() -> None:
    launcher = FakeLauncher()
    settings = Settings(openai_api_key=None)

    assistant = build_assistant(settings=settings, launcher=launcher)

    assert assistant.handle("open chrome").success
    assert not assistant.handle("Could you open Chrome for me?").success
    assert launcher.apps[0].display_name == "Chrome"


def test_missing_api_key_disables_optional_voice_providers() -> None:
    services = build_voice_services(Settings(openai_api_key=None))

    assert services.transcription is None
    assert services.speech is None
