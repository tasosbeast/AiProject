from __future__ import annotations

from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog
from desktop_assistant.launcher import WindowsSystemLauncher
from desktop_assistant.router import CommandRouter
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


def build_assistant() -> Assistant:
    """Compose the production assistant shared by every user interface."""

    launcher = WindowsSystemLauncher()
    catalog = AppCatalog()
    router = CommandRouter(
        OpenAppTool(launcher, catalog),
        OpenFolderTool(launcher),
        OpenWebsiteTool(launcher),
    )
    return Assistant(router)
