from __future__ import annotations

import re

from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.safety import SafetyPolicy
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


class CommandRouter:
    """Deterministic, deliberately narrow command parser."""

    _website = re.compile(r"^open\s+(?:website\s+|site\s+)?(https?://\S+)\s*$", re.IGNORECASE)
    _folder = re.compile(r"^open\s+folder\s+(.+)$", re.IGNORECASE)
    _app = re.compile(r"^open\s+(?:app\s+)?(.+?)\s*$", re.IGNORECASE)

    def __init__(
        self,
        open_app: OpenAppTool,
        open_folder: OpenFolderTool,
        open_website: OpenWebsiteTool,
        safety_policy: SafetyPolicy | None = None,
    ) -> None:
        self._open_app = open_app
        self._open_folder = open_folder
        self._open_website = open_website
        self._safety_policy = safety_policy or SafetyPolicy()

    def _execute(self, tool: object, argument: str) -> ToolResult:
        risk_level = getattr(tool, "risk_level")
        denial = self._safety_policy.authorize(risk_level)
        if denial is not None:
            return denial
        return tool.run(argument)  # type: ignore[attr-defined, no-any-return]

    def route(self, command: str) -> ToolResult:
        text = command.strip()
        if not text:
            return ToolResult(False, "Please enter a command.", RiskLevel.SAFE)
        if text.casefold() in {"help", "?"}:
            return ToolResult(True, self.help_text(), RiskLevel.SAFE)

        match = self._website.fullmatch(text)
        if match:
            return self._execute(self._open_website, match.group(1))

        match = self._folder.fullmatch(text)
        if match:
            return self._execute(self._open_folder, match.group(1))

        match = self._app.fullmatch(text)
        if match:
            return self._execute(self._open_app, match.group(1))

        return ToolResult(
            False,
            "I did not understand that command. Type 'help' to see supported commands.",
            RiskLevel.SAFE,
        )

    @staticmethod
    def help_text() -> str:
        return (
            "Supported commands:\n"
            "  open <Chrome|Spotify|VS Code|File Explorer|Notepad>\n"
            "  open folder <existing path>\n"
            "  open website <http-or-https URL>\n"
            "  help\n"
            "  exit"
        )
