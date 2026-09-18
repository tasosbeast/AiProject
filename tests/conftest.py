from __future__ import annotations

from pathlib import Path

from desktop_assistant.config import AppCatalog, AppDefinition
from desktop_assistant.known_folders import KnownFolderResolver
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


def make_registry(
    launcher: FakeLauncher,
    *,
    safety_policy: SafetyPolicy | None = None,
    home: Path | None = None,
) -> ToolRegistry:
    catalog = AppCatalog()
    return ToolRegistry(
        default_tool_definitions(
            OpenAppTool(launcher, catalog),
            OpenFolderTool(launcher),
            OpenWebsiteTool(launcher),
        ),
        safety_policy=safety_policy,
        known_folders=KnownFolderResolver(home),
    )
