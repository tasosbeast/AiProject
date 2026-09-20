from __future__ import annotations

from pathlib import Path

import pytest

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog
from desktop_assistant.filesystem import FilesystemPathValidator
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    RenamePathTool,
)
from desktop_assistant.intent.models import IntentResult
from desktop_assistant.intent.provider import IntentProviderUnavailableError
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool

from conftest import FakeLauncher, FakeProcessController


class FakeProvider:
    def __init__(self, result: IntentResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def resolve(self, request: str) -> IntentResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def make_assistant(
    launcher: FakeLauncher,
    provider: FakeProvider | None,
    *,
    home: Path | None = None,
    process_controller: FakeProcessController | None = None,
) -> Assistant:
    catalog = AppCatalog()
    validator = FilesystemPathValidator()
    process_controller = process_controller or FakeProcessController()
    registry = ToolRegistry(
        default_tool_definitions(
            OpenAppTool(launcher, catalog),
            OpenFolderTool(launcher),
            OpenWebsiteTool(launcher),
            AppStatusTool(catalog, process_controller),
            CloseAppTool(catalog, process_controller),
            ListFolderTool(validator),
            PathExistsTool(validator),
            CreateFolderTool(validator),
            RenamePathTool(validator),
            MovePathTool(validator),
        ),
        known_folders=KnownFolderResolver(home),
    )
    return Assistant(CommandRouter(registry, catalog), registry, provider)


def test_exact_command_does_not_invoke_ai() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.tool_action("open_app", {"app_name": "Chrome"}))

    result = make_assistant(launcher, provider).handle("open spotify")

    assert result.success
    assert provider.calls == []
    assert launcher.apps[0].display_name == "Spotify"


@pytest.mark.parametrize(
    "user_request",
    (
        "Could you open Spotify for me?",
        "Vale mou to Spotify.",
        "Θέλω να ανοίξεις το Spotify.",
    ),
)
def test_natural_language_app_intents_use_registered_tool(user_request: str) -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.tool_action("open_app", {"app_name": "Spotify"}))

    result = make_assistant(launcher, provider).handle(user_request)

    assert result.success
    assert provider.calls == [user_request]
    assert launcher.apps[0].display_name == "Spotify"


def test_natural_language_website_intent_uses_existing_validation() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.tool_action("open_website", {"url": "https://python.org"})
    )

    result = make_assistant(launcher, provider).handle("Go to python.org.")

    assert result.success
    assert launcher.websites == ["https://python.org"]


def test_known_folder_intent_resolves_without_search(tmp_path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.tool_action("open_folder", {"path": "Downloads"}))

    result = make_assistant(launcher, provider, home=tmp_path).handle("Open my Downloads folder.")

    assert result.success
    assert launcher.folders == [downloads.resolve()]


@pytest.mark.parametrize(
    "intent",
    (
        IntentResult.tool_action("made_up_tool", {"value": "x"}),
        IntentResult.tool_action("open_app", {}),
        IntentResult.tool_action("open_app", {"app_name": 3}),
        IntentResult.tool_action("open_app", {"app_name": "Spotify", "extra": "x"}),
        IntentResult.tool_action("open_website", {"url": "file:///C:/Windows"}),
        IntentResult.tool_action("open_app", {"app_name": "Unknown App"}),
    ),
)
def test_untrusted_provider_actions_execute_nothing(intent: IntentResult) -> None:
    launcher = FakeLauncher()

    result = make_assistant(launcher, FakeProvider(intent)).handle("natural request")

    assert not result.success
    assert launcher.apps == []
    assert launcher.folders == []
    assert launcher.websites == []


def test_unsupported_intent_executes_nothing() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.unsupported("I cannot delete files."))

    result = make_assistant(launcher, provider).handle("Delete everything in Downloads.")

    assert not result.success
    assert launcher.apps == []
    assert launcher.folders == []
    assert launcher.websites == []


def test_conversation_intent_executes_nothing() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.conversation("I can open approved apps, folders, and websites."))

    result = make_assistant(launcher, provider).handle("What can you do?")

    assert result.success
    assert launcher.apps == []
    assert launcher.folders == []
    assert launcher.websites == []


def test_missing_provider_preserves_deterministic_behavior() -> None:
    launcher = FakeLauncher()
    assistant = make_assistant(launcher, None)

    assert assistant.handle("open notepad").success
    assert not assistant.handle("Could you open Notepad?").success
    assert launcher.apps[0].display_name == "Notepad"


def test_provider_failure_returns_safe_user_message() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(error=IntentProviderUnavailableError("technical detail"))

    result = make_assistant(launcher, provider).handle("Could you open Chrome?")

    assert not result.success
    assert result.message == "AI routing is temporarily unavailable."
    assert "technical detail" not in result.message


def test_recognized_rejection_never_falls_through_to_ai() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.tool_action("open_app", {"app_name": "Spotify"}))

    result = make_assistant(launcher, provider).handle("open malware.exe")

    assert not result.success
    assert provider.calls == []
    assert launcher.apps == []


def test_natural_sensitive_action_calls_provider_once_across_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "draft.txt"
    destination = tmp_path / "final.txt"
    source.write_text("draft", encoding="utf-8")
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.tool_action(
            "rename_path",
            {"source": str(source), "destination": str(destination)},
        )
    )
    assistant = make_assistant(launcher, provider)

    response = assistant.handle("Μετονόμασε το draft.txt σε final.txt.")
    assert response.confirmation is not None
    result = assistant.confirm(response.confirmation.confirmation_id)

    assert result.success
    assert provider.calls == ["Μετονόμασε το draft.txt σε final.txt."]
    assert not source.exists() and destination.exists()


def test_natural_safe_filesystem_query_does_not_prompt(tmp_path: Path) -> None:
    provider = FakeProvider(IntentResult.tool_action("list_folder", {"path": str(tmp_path)}))
    assistant = make_assistant(FakeLauncher(), provider)

    response = assistant.handle("Τι έχει μέσα αυτός ο ακριβής φάκελος;")

    assert response.success
    assert response.confirmation is None


@pytest.mark.parametrize(
    "user_request",
    ("Τρέχει το Spotify;", "Trexei to Spotify?", "Is Spotify running?"),
)
def test_natural_app_status_uses_safe_registered_tool(user_request: str) -> None:
    provider = FakeProvider(IntentResult.tool_action("app_status", {"app_name": "Spotify"}))
    controller = FakeProcessController({"Spotify"})
    assistant = make_assistant(
        FakeLauncher(), provider, process_controller=controller
    )

    response = assistant.handle(user_request)

    assert response.success
    assert response.confirmation is None
    assert response.message == "Spotify is running."
    assert provider.calls == [user_request]


def test_natural_close_app_calls_provider_once_across_confirmation() -> None:
    provider = FakeProvider(IntentResult.tool_action("close_app", {"app_name": "Notepad"}))
    controller = FakeProcessController({"Notepad"})
    assistant = make_assistant(
        FakeLauncher(), provider, process_controller=controller
    )

    response = assistant.handle("Κλείσε το Notepad.")
    assert response.confirmation is not None
    result = assistant.confirm(response.confirmation.confirmation_id)

    assert result.success
    assert provider.calls == ["Κλείσε το Notepad."]
    assert [app.display_name for app in controller.close_calls] == ["Notepad"]


def test_cancelled_assistant_handle_prevents_safe_tool_execution() -> None:
    from desktop_assistant.cancellation import CancellationToken

    launcher = FakeLauncher()
    provider = FakeProvider(IntentResult.tool_action("open_app", {"app_name": "Notepad"}))
    assistant = make_assistant(launcher, provider)
    token = CancellationToken()
    token.cancel()

    response = assistant.handle("Open notepad please", cancellation_token=token)

    assert not response.success
    assert response.confirmation is None
    assert launcher.apps == []


def test_cancelled_assistant_handle_prevents_sensitive_confirmation_creation(tmp_path: Path) -> None:
    from desktop_assistant.cancellation import CancellationToken

    target = tmp_path / "new_dir"
    provider = FakeProvider(IntentResult.tool_action("create_folder", {"path": str(target)}))
    assistant = make_assistant(FakeLauncher(), provider)
    token = CancellationToken()
    token.cancel()

    response = assistant.handle("Create a folder here", cancellation_token=token)

    assert not response.success
    assert response.confirmation is None
    assert not target.exists()
    assert not assistant.has_pending_confirmation()


def test_shutdown_assistant_handle_prevents_all_tool_execution(tmp_path: Path) -> None:
    target = tmp_path / "new_dir"
    provider = FakeProvider(IntentResult.tool_action("create_folder", {"path": str(target)}))
    assistant = make_assistant(FakeLauncher(), provider)
    assistant.shutdown()

    response = assistant.handle("Create a folder here")

    assert not response.success
    assert response.confirmation is None
    assert not target.exists()
    assert not assistant.has_pending_confirmation()
