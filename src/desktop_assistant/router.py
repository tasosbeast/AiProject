from __future__ import annotations

import re
from dataclasses import dataclass

from typing import Any

from desktop_assistant.config import AppCatalog
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.tool_registry import RegistryOutcome, ToolRegistry


@dataclass(frozen=True, slots=True)
class DeterministicAction:
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    recognized: bool
    action: DeterministicAction | None = None
    direct_result: ToolResult | None = None
    fallback_result: ToolResult | None = None

    @property
    def result(self) -> RegistryOutcome:
        if self.direct_result is not None:
            return self.direct_result
        if self.fallback_result is not None:
            return self.fallback_result
        if self.action is not None:
            raise RuntimeError(
                f"RouteDecision resolved to action '{self.action.tool_name}', "
                "which must be executed via ToolRegistry after the cancellation boundary."
            )
        return ToolResult(False, "I did not understand that command.", RiskLevel.SAFE)


class CommandRouter:
    """Deterministic, deliberately narrow command parser."""

    _website = re.compile(r"^open\s+(?:website|site)\s+(.+?)\s*$", re.IGNORECASE)
    _direct_url = re.compile(r"^open\s+(https?://\S+)\s*$", re.IGNORECASE)
    _folder = re.compile(r"^open\s+folder\s+(.+)$", re.IGNORECASE)
    _list_folder = re.compile(r"^list\s+folder\s+(.+)$", re.IGNORECASE)
    _check_path = re.compile(r"^(?:check\s+path|path\s+exists)\s+(.+)$", re.IGNORECASE)
    _app_status = re.compile(r"^(?:check\s+app|app\s+status)\s+(.+?)\s*$", re.IGNORECASE)
    _close_app = re.compile(r"^close\s+app\s+(.+?)\s*$", re.IGNORECASE)
    _explicit_app = re.compile(r"^open\s+app\s+(.+?)\s*$", re.IGNORECASE)
    _simple_app = re.compile(r"^open\s+(.+?)\s*$", re.IGNORECASE)

    def __init__(self, registry: ToolRegistry | None = None, catalog: AppCatalog | None = None) -> None:
        self._registry = registry
        self._catalog = catalog or AppCatalog()

    def route(self, command: str) -> RegistryOutcome:
        decision = self.route_detailed(command)
        if decision.direct_result is not None:
            return decision.direct_result
        if decision.action is not None and self._registry is not None:
            return self._registry.execute(decision.action.tool_name, decision.action.arguments)
        if decision.fallback_result is not None:
            return decision.fallback_result
        return ToolResult(
            False,
            "I did not understand that command. Type 'help' to see supported commands.",
            RiskLevel.SAFE,
        )

    def route_detailed(self, command: str) -> RouteDecision:
        text = command.strip()
        if not text:
            return RouteDecision(
                recognized=True,
                direct_result=ToolResult(False, "Please enter a command.", RiskLevel.SAFE),
            )
        if text.casefold() in {"help", "?"}:
            return RouteDecision(
                recognized=True,
                direct_result=ToolResult(True, self.help_text(), RiskLevel.SAFE),
            )

        match = self._website.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("open_website", {"url": match.group(1)}),
            )

        match = self._direct_url.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("open_website", {"url": match.group(1)}),
            )

        match = self._folder.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("open_folder", {"path": match.group(1)}),
            )

        match = self._list_folder.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("list_folder", {"path": match.group(1)}),
            )

        match = self._check_path.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("path_exists", {"path": match.group(1)}),
            )

        match = self._app_status.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("app_status", {"app_name": match.group(1)}),
            )

        match = self._close_app.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("close_app", {"app_name": match.group(1)}),
            )

        match = self._explicit_app.fullmatch(text)
        if match:
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("open_app", {"app_name": match.group(1)}),
            )

        match = self._simple_app.fullmatch(text)
        if match:
            candidate = match.group(1)
            if self._catalog.resolve(candidate) is not None or self._looks_unsafe_app_target(candidate):
                return RouteDecision(
                    recognized=True,
                    action=DeterministicAction("open_app", {"app_name": candidate}),
                )

        return RouteDecision(
            recognized=False,
            fallback_result=ToolResult(
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
            "  list folder <existing path>\n"
            "  check path <path>\n"
            "  check app <Chrome|Spotify|VS Code|File Explorer|Notepad>\n"
            "  close app <Chrome|Spotify|VS Code|Notepad>\n"
            "  open website <http-or-https URL>\n"
            "  help\n"
            "  exit"
        )
