from __future__ import annotations

import re
from dataclasses import dataclass

from desktop_assistant.config import AppCatalog
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.tool_registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class RouteDecision:
    recognized: bool
    result: ToolResult


class CommandRouter:
    """Deterministic, deliberately narrow command parser."""

    _website = re.compile(r"^open\s+(?:website|site)\s+(.+?)\s*$", re.IGNORECASE)
    _direct_url = re.compile(r"^open\s+(https?://\S+)\s*$", re.IGNORECASE)
    _folder = re.compile(r"^open\s+folder\s+(.+)$", re.IGNORECASE)
    _explicit_app = re.compile(r"^open\s+app\s+(.+?)\s*$", re.IGNORECASE)
    _simple_app = re.compile(r"^open\s+(.+?)\s*$", re.IGNORECASE)

    def __init__(self, registry: ToolRegistry, catalog: AppCatalog) -> None:
        self._registry = registry
        self._catalog = catalog

    def route(self, command: str) -> ToolResult:
        return self.route_detailed(command).result

    def route_detailed(self, command: str) -> RouteDecision:
        text = command.strip()
        if not text:
            return RouteDecision(
                True,
                ToolResult(False, "Please enter a command.", RiskLevel.SAFE),
            )
        if text.casefold() in {"help", "?"}:
            return RouteDecision(
                True,
                ToolResult(True, self.help_text(), RiskLevel.SAFE),
            )

        match = self._website.fullmatch(text)
        if match:
            return RouteDecision(
                True,
                self._registry.execute("open_website", {"url": match.group(1)}),
            )

        match = self._direct_url.fullmatch(text)
        if match:
            return RouteDecision(
                True,
                self._registry.execute("open_website", {"url": match.group(1)}),
            )

        match = self._folder.fullmatch(text)
        if match:
            return RouteDecision(
                True,
                self._registry.execute("open_folder", {"path": match.group(1)}),
            )

        match = self._explicit_app.fullmatch(text)
        if match:
            return RouteDecision(
                True,
                self._registry.execute("open_app", {"app_name": match.group(1)}),
            )

        match = self._simple_app.fullmatch(text)
        if match:
            candidate = match.group(1)
            if self._catalog.resolve(candidate) is not None or self._looks_unsafe_app_target(candidate):
                return RouteDecision(
                    True,
                    self._registry.execute("open_app", {"app_name": candidate}),
                )

        return RouteDecision(
            False,
            ToolResult(
                False,
                "I did not understand that command. Type 'help' to see supported commands.",
                RiskLevel.SAFE,
            ),
        )

    @staticmethod
    def _looks_unsafe_app_target(value: str) -> bool:
        lowered = value.casefold().strip()
        return (
            lowered.endswith(".exe")
            or "\\" in value
            or "/" in value
            or "--" in value
            or bool(re.search(r"[;&|<>]", value))
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
