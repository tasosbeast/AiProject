from __future__ import annotations

from desktop_assistant.config import AppCatalog
from desktop_assistant.router import CommandRouter
from conftest import FakeLauncher, make_registry


def make_router(launcher: FakeLauncher) -> CommandRouter:
    return CommandRouter(make_registry(launcher), AppCatalog())


def test_routes_application_alias_case_insensitively() -> None:
    launcher = FakeLauncher()

    result = make_router(launcher).route("OPEN visual studio code")

    assert result.success
    assert launcher.apps[0].display_name == "VS Code"


def test_routes_website_before_generic_app_command() -> None:
    launcher = FakeLauncher()

    result = make_router(launcher).route("open website https://example.com")

    assert result.success
    assert launcher.websites == ["https://example.com"]
    assert launcher.apps == []


def test_rejects_arbitrary_shell_commands() -> None:
    launcher = FakeLauncher()

    result = make_router(launcher).route("run powershell Remove-Item C:\\data")

    assert not result.success
    assert launcher.apps == []
    assert launcher.folders == []
    assert launcher.websites == []


def test_recognized_unknown_executable_is_not_an_unmatched_command() -> None:
    decision = make_router(FakeLauncher()).route_detailed("open malware.exe")

    assert decision.recognized
    assert not decision.result.success


def test_natural_language_request_is_left_for_optional_provider() -> None:
    decision = make_router(FakeLauncher()).route_detailed("Could you open Spotify for me?")

    assert not decision.recognized


def test_help_lists_supported_commands() -> None:
    result = make_router(FakeLauncher()).route("help")

    assert result.success
    assert "open folder" in result.message
    assert "open website" in result.message
