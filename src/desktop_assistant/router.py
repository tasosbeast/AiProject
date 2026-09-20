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
    _volume_up = re.compile(
        r"^(?:volume\s+up|increase\s+volume|turn\s+up(?:\s+the)?\s+volume)$",
        re.IGNORECASE,
    )
    _volume_down = re.compile(
        r"^(?:volume\s+down|decrease\s+volume|turn\s+down(?:\s+the)?\s+volume)$",
        re.IGNORECASE,
    )
    _mute = re.compile(
        r"^(?:mute|unmute|toggle\s+mute|mute\s+volume)$",
        re.IGNORECASE,
    )
    _play_pause = re.compile(
        r"^(?:play|pause|play\s*/\s*pause|play\s+pause|resume|toggle\s+playback)$",
        re.IGNORECASE,
    )
    _next_track = re.compile(
        r"^(?:next\s+track|next\s+song|skip\s+track|skip\s+song)$",
        re.IGNORECASE,
    )
    _prev_track = re.compile(
        r"^(?:previous\s+track|previous\s+song|prev\s+track|prev\s+song)$",
        re.IGNORECASE,
    )
    _cpu_status = re.compile(
        r"^(?:cpu|check\s+cpu|cpu\s+usage)$",
        re.IGNORECASE,
    )
    _memory_status = re.compile(
        r"^(?:ram|memory|check\s+ram|check\s+memory|ram\s+usage|memory\s+usage)$",
        re.IGNORECASE,
    )
    _battery_status = re.compile(
        r"^(?:battery|check\s+battery|battery\s+status)$",
        re.IGNORECASE,
    )
    _disk_status = re.compile(
        r"^(?:disk|check\s+disk|disk\s+space|check\s+disk\s+space)$",
        re.IGNORECASE,
    )
    _overview_status = re.compile(
        r"^(?:system\s+status|system\s+overview|check\s+system|overview|system)$",
        re.IGNORECASE,
    )
    _open_project = re.compile(
        r"^(?:open\s+project\s+aiproject|open\s+aiproject)$",
        re.IGNORECASE,
    )
    _run_tests = re.compile(
        r"^(?:run\s+tests|run\s+aiproject\s+tests|test\s+aiproject)$",
        re.IGNORECASE,
    )
    _window_list = re.compile(
        r"^(?:list\s+(?:open\s+)?windows|show\s+windows|windows)$",
        re.IGNORECASE,
    )
    _window_active = re.compile(
        r"^(?:active\s+window|current\s+window|what(?:\'?s|\s+is)\s+the\s+active\s+window)$",
        re.IGNORECASE,
    )
    _window_focus = re.compile(
        r"^(?:focus|switch\s+to)\s+(.+)$",
        re.IGNORECASE,
    )

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

        if self._volume_up.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("volume_control", {"action": "volume_up"}),
            )

        if self._volume_down.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("volume_control", {"action": "volume_down"}),
            )

        if self._mute.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("volume_control", {"action": "mute_toggle"}),
            )

        if self._play_pause.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("media_control", {"action": "play_pause"}),
            )

        if self._next_track.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("media_control", {"action": "next_track"}),
            )

        if self._prev_track.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("media_control", {"action": "previous_track"}),
            )

        if self._cpu_status.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("system_status", {"metric": "cpu"}),
            )

        if self._memory_status.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("system_status", {"metric": "memory"}),
            )

        if self._battery_status.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("system_status", {"metric": "battery"}),
            )

        if self._disk_status.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("system_status", {"metric": "disk"}),
            )

        if self._overview_status.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("system_status", {"metric": "overview"}),
            )

        if self._open_project.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("open_project", {"project_name": "AiProject"}),
            )

        if self._run_tests.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("run_project_task", {"project_name": "AiProject", "task": "tests"}),
            )

        if self._window_list.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("window_info", {"action": "list"}),
            )

        if self._window_active.fullmatch(text):
            return RouteDecision(
                recognized=True,
                action=DeterministicAction("window_info", {"action": "active"}),
            )

        focus_match = self._window_focus.fullmatch(text)
        if focus_match:
            query = focus_match.group(1).strip()
            if query:
                return RouteDecision(
                    recognized=True,
                    action=DeterministicAction("focus_window", {"query": query}),
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
            "  open aiproject\n"
            "  run tests\n"
            "  volume up | volume down | mute\n"
            "  play | pause | next track | previous track\n"
            "  cpu | memory | battery | disk | system status\n"
            "  windows | active window | focus <name>\n"
            "  help\n"
            "  exit"
        )
