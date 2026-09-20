from __future__ import annotations

from desktop_assistant.config import AppCatalog
from desktop_assistant.router import CommandRouter
from desktop_assistant.models import ConfirmationRequest, ToolResult
from conftest import FakeLauncher, FakeProcessController, make_registry


def make_router(
    launcher: FakeLauncher,
    process_controller: FakeProcessController | None = None,
) -> CommandRouter:
    return CommandRouter(
        make_registry(launcher, process_controller=process_controller),
        AppCatalog(),
    )


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
    assert "list folder" in result.message
    assert "check path" in result.message
    assert "check app" in result.message
    assert "close app" in result.message


def test_routes_safe_filesystem_queries_deterministically(tmp_path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    router = make_router(FakeLauncher())

    listing = router.route(f"list folder {tmp_path}")
    exists = router.route(f"check path {tmp_path / 'file.txt'}")

    assert listing.success and "file.txt" in listing.message
    assert exists.success and "file" in exists.message


def test_routes_app_status_and_close_app_deterministically() -> None:
    controller = FakeProcessController({"Spotify"})
    router = make_router(FakeLauncher(), controller)

    status = router.route("app status spotify")
    close = router.route("close app spotify")

    assert isinstance(status, ToolResult) and status.success
    assert isinstance(close, ConfirmationRequest)
    assert controller.close_calls == []


def test_recognized_invalid_close_app_never_falls_through_to_provider() -> None:
    decision = make_router(FakeLauncher()).route_detailed("close app explorer")

    assert decision.recognized
    assert isinstance(decision.result, ToolResult) and not decision.result.success
