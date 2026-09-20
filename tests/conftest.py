from __future__ import annotations

from pathlib import Path

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.config import AppCatalog, AppDefinition
from desktop_assistant.filesystem import FilesystemPathValidator
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    RenamePathTool,
)
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.process_control import CloseRequestResult
from desktop_assistant.safety import SafetyPolicy
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


class FakeLauncher:
    def __init__(self) -> None:
        self.apps: list[AppDefinition] = []
        self.folders: list[Path] = []
        self.websites: list[str] = []

    def launch_app(self, app: AppDefinition) -> None:
        self.apps.append(app)

    def open_folder(self, path: Path) -> None:
        self.folders.append(path)

    def open_website(self, url: str) -> None:
        self.websites.append(url)


class FakeProcessController:
    def __init__(self, running: set[str] | None = None) -> None:
        self.running = {name.casefold() for name in (running or set())}
        self.status_calls: list[AppDefinition] = []
        self.close_calls: list[AppDefinition] = []
        self.requested_window_count = 1

    def is_running(self, app: AppDefinition) -> bool:
        self.status_calls.append(app)
        return app.display_name.casefold() in self.running

    def request_close(self, app: AppDefinition) -> CloseRequestResult:
        self.close_calls.append(app)
        return CloseRequestResult(
            app.display_name.casefold() in self.running,
            self.requested_window_count,
        )


def make_registry(
    launcher: FakeLauncher,
    *,
    safety_policy: SafetyPolicy | None = None,
    home: Path | None = None,
    process_controller: FakeProcessController | None = None,
) -> ToolRegistry:
    catalog = AppCatalog()
    validator = FilesystemPathValidator()
    process_controller = process_controller or FakeProcessController()
    return ToolRegistry(
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
        safety_policy=safety_policy,
        known_folders=KnownFolderResolver(home),
    )
