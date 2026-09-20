"""Live non-executing routing smoke test for OpenAIIntentProvider.

Exercises real model routing without executing any local tools or opening applications.
Never logs or prints the OpenAI API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Force UTF-8 stdout for Greek characters on Windows
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# Ensure repository root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from desktop_assistant.config import load_settings
from desktop_assistant.intent.models import IntentKind
from desktop_assistant.intent.openai_provider import OpenAIIntentProvider
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    RenamePathTool,
)
from desktop_assistant.media_control import MediaControlTool, VolumeControlTool
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


class NoopLauncher:
    def launch_app(self, app: object) -> None:
        raise AssertionError("Live smoke test must NEVER execute launch_app")

    def open_folder(self, path: object) -> None:
        raise AssertionError("Live smoke test must NEVER execute open_folder")

    def open_website(self, url: object) -> None:
        raise AssertionError("Live smoke test must NEVER execute open_website")


class NoopProcessController:
    def is_running(self, app: object) -> bool:
        return False

    def request_close(self, app: object) -> object:
        raise AssertionError("Live smoke test must NEVER execute request_close")


class NoopValidator:
    def validate_local_path(self, path: object) -> object:
        raise AssertionError("Live smoke test must NEVER execute filesystem operations")


class NoopMediaController:
    def send_volume(self, action: object) -> None:
        raise AssertionError("Live smoke test must NEVER execute volume control")

    def send_media(self, action: object) -> None:
        raise AssertionError("Live smoke test must NEVER execute media control")


def make_smoke_registry() -> ToolRegistry:
    from desktop_assistant.config import AppCatalog

    launcher = NoopLauncher()
    catalog = AppCatalog()
    controller = NoopProcessController()
    validator = NoopValidator()
    media_controller = NoopMediaController()
    return ToolRegistry(
        default_tool_definitions(
            OpenAppTool(launcher, catalog),  # type: ignore[arg-type]
            OpenFolderTool(launcher),  # type: ignore[arg-type]
            OpenWebsiteTool(launcher),  # type: ignore[arg-type]
            AppStatusTool(catalog, controller),  # type: ignore[arg-type]
            CloseAppTool(catalog, controller),  # type: ignore[arg-type]
            ListFolderTool(validator),  # type: ignore[arg-type]
            PathExistsTool(validator),  # type: ignore[arg-type]
            CreateFolderTool(validator),  # type: ignore[arg-type]
            RenamePathTool(validator),  # type: ignore[arg-type]
            MovePathTool(validator),  # type: ignore[arg-type]
            VolumeControlTool(media_controller),  # type: ignore[arg-type]
            MediaControlTool(media_controller),  # type: ignore[arg-type]
        )
    )


PROMPTS: list[tuple[str, IntentKind, int, list[str]]] = [
    ("Άνοιξε το Chrome και το Spotify.", IntentKind.ACTION_PLAN, 2, ["open_app", "open_app"]),
    (
        "Άνοιξε το Chrome, το Spotify και το Notepad.",
        IntentKind.ACTION_PLAN,
        3,
        ["open_app", "open_app", "open_app"],
    ),
    ("Άνοιξε Chrome και μετά open Spotify.", IntentKind.ACTION_PLAN, 2, ["open_app", "open_app"]),
    ("Open Chrome and Spotify.", IntentKind.ACTION_PLAN, 2, ["open_app", "open_app"]),
    ("Anoikse Chrome kai Spotify.", IntentKind.ACTION_PLAN, 2, ["open_app", "open_app"]),
    ("Άνοιξε το Chrome.", IntentKind.TOOL_ACTION, 1, ["open_app"]),
]


def run_smoke_test() -> int:
    settings = load_settings()
    if not settings.openai_api_key:
        print("ERROR: OpenAI API key is not configured. Cannot run live smoke test.")
        return 1

    registry = make_smoke_registry()
    provider = OpenAIIntentProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        tool_schemas=registry.schemas(),
        timeout_seconds=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )

    print(f"Running live routing smoke test against model '{settings.openai_model}'...")
    all_passed = True

    for idx, (prompt, expected_kind, expected_count, expected_tools) in enumerate(PROMPTS, 1):
        print(f"\n[{idx}/6] Prompt: {prompt}")
        result = provider.resolve(prompt)
        print(f"      Kind: {result.kind.value}")

        if result.kind is IntentKind.TOOL_ACTION:
            actual_tools = [result.action.tool_name] if result.action else []
            actual_args = [dict(result.action.arguments)] if result.action else []
            print(f"      Tool: {actual_tools}")
            print(f"      Arguments: {actual_args}")
        elif result.kind is IntentKind.ACTION_PLAN:
            actual_tools = [a.tool_name for a in result.plan.actions] if result.plan else []
            actual_args = [dict(a.arguments) for a in result.plan.actions] if result.plan else []
            print(f"      Steps: {len(actual_tools)}")
            print(f"      Tools: {actual_tools}")
            print(f"      Arguments: {actual_args}")
        else:
            print(f"      Message: {result.message}")
            actual_tools = []

        if result.kind != expected_kind:
            print(f"      FAIL: Expected {expected_kind.value}, got {result.kind.value}")
            all_passed = False
            continue

        if actual_tools != expected_tools:
            print(f"      FAIL: Expected tools {expected_tools}, got {actual_tools}")
            all_passed = False
            continue

        print("      PASS")

    if all_passed:
        print("\nAll 6 live routing smoke tests PASSED successfully!")
        return 0
    else:
        print("\nOne or more live routing smoke tests FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(run_smoke_test())
