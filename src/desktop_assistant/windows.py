from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Sequence

from desktop_assistant.config import AppCatalog
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult
from desktop_assistant.process_control import (
    WindowController,
    WindowInfo,
    WindowsWindowController,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedWindowFocus:
    handle: int
    process_id: int
    title: str
    executable_name: str


def match_window_for_focus(
    query: str,
    windows: Sequence[WindowInfo],
    catalog: AppCatalog,
) -> WindowInfo | str:
    q = query.strip()
    if not q:
        return "Window query must not be empty."

    if not windows:
        return "No visible windows found."

    q_case = q.casefold()

    # 1. Exact normalized title match first
    exact_title_matches = [w for w in windows if w.title.strip().casefold() == q_case]
    if exact_title_matches:
        return exact_title_matches[0]

    # 2. AppCatalog aliases such as "VS Code", "Chrome", "Spotify", "Notepad"
    # and match their trusted process_names
    app = catalog.resolve(q)
    if app is not None:
        trusted_processes = {p.casefold() for p in app.process_names}
        app_windows = [
            w for w in windows if w.executable_name.casefold() in trusted_processes
        ]
        if app_windows:
            # Choose the first/top-most visible matching window deterministically
            return app_windows[0]

    # 3. Case-insensitive title substring matching
    sub_matches = [w for w in windows if q_case in w.title.casefold()]
    if len(sub_matches) == 1:
        return sub_matches[0]
    elif len(sub_matches) > 1:
        candidates = ", ".join(f"'{w.title}'" for w in sub_matches[:3])
        return f"Multiple windows match '{query}': {candidates}. Please specify the exact title."

    # 4. Optionally exact executable stem matching (e.g. query "spotify" matches "spotify.exe")
    stem_matches = [
        w for w in windows
        if w.executable_name.casefold().removesuffix(".exe") == q_case
    ]
    if len(stem_matches) == 1:
        return stem_matches[0]
    elif len(stem_matches) > 1:
        candidates = ", ".join(f"'{w.title}'" for w in stem_matches[:3])
        return f"Multiple windows match '{query}': {candidates}. Please specify the exact title."

    return f"No matching window found for '{query}'."


class WindowInfoTool:
    name = "window_info"
    risk_level = RiskLevel.SAFE
    allowed_actions = ("list", "active")

    def __init__(self, controller: WindowController) -> None:
        self._controller = controller

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        action = arguments.get("action")
        if action not in self.allowed_actions:
            supported = ", ".join(self.allowed_actions)
            return ToolResult(
                False,
                f"Unsupported window_info action '{action}'. Supported actions: {supported}.",
                self.risk_level,
            )
        return ToolPreparation(str(action), ToolArguments((("action", str(action)),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, str):
            return ToolResult(False, "The prepared window action is invalid.", self.risk_level)

        if prepared_value == "list":
            try:
                windows = self._controller.visible_windows()
            except Exception:
                logger.exception("Failed to inspect visible windows.")
                return ToolResult(False, "Failed to inspect visible windows.", self.risk_level)

            if not windows:
                return ToolResult(
                    True,
                    "No visible windows found.",
                    self.risk_level,
                    {"windows": []},
                )

            lines = [f"- {w.title} — {w.executable_name}" for w in windows]
            message = "Visible windows:\n" + "\n".join(lines)
            details = {
                "windows": [
                    {"title": w.title, "executable": w.executable_name}
                    for w in windows
                ]
            }
            return ToolResult(True, message, self.risk_level, details)

        if prepared_value == "active":
            try:
                active = self._controller.get_foreground_window()
            except Exception:
                logger.exception("Failed to inspect active window.")
                return ToolResult(False, "Failed to inspect active window.", self.risk_level)

            if active is None or not active.title:
                return ToolResult(True, "No active window detected.", self.risk_level, {})

            message = f"Active window: {active.title} — {active.executable_name}"
            details = {"title": active.title, "executable": active.executable_name}
            return ToolResult(True, message, self.risk_level, details)

        return ToolResult(False, f"Unsupported action: {prepared_value}", self.risk_level)


class FocusWindowTool:
    name = "focus_window"
    risk_level = RiskLevel.SAFE

    def __init__(self, controller: WindowController, catalog: AppCatalog) -> None:
        self._controller = controller
        self._catalog = catalog

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        query = arguments.get("query")
        if not query or not isinstance(query, str) or not query.strip():
            return ToolResult(False, "Window query must not be empty.", self.risk_level)

        query_str = query.strip()
        try:
            windows = self._controller.visible_windows()
        except Exception:
            logger.exception("Failed to inspect visible windows during focus preparation.")
            return ToolResult(False, "Failed to inspect visible windows.", self.risk_level)

        match_result = match_window_for_focus(query_str, windows, self._catalog)
        if isinstance(match_result, str):
            return ToolResult(False, match_result, self.risk_level)

        prepared = PreparedWindowFocus(
            handle=match_result.handle,
            process_id=match_result.process_id,
            title=match_result.title,
            executable_name=match_result.executable_name,
        )
        return ToolPreparation(prepared, ToolArguments((("query", query_str),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedWindowFocus):
            return ToolResult(False, "The prepared focus action is invalid.", self.risk_level)

        try:
            is_valid = self._controller.is_window_valid(
                prepared_value.handle, prepared_value.process_id
            )
        except Exception:
            logger.exception("Failed to validate window %s.", prepared_value.title)
            return ToolResult(
                False,
                f"Window '{prepared_value.title}' is no longer available.",
                self.risk_level,
            )

        if not is_valid:
            return ToolResult(
                False,
                f"Window '{prepared_value.title}' is no longer available.",
                self.risk_level,
            )

        try:
            if self._controller.is_minimized(prepared_value.handle):
                self._controller.restore_window(prepared_value.handle)

            focused = self._controller.set_foreground_window(prepared_value.handle)
        except Exception:
            logger.exception("Failed to bring window %s to foreground.", prepared_value.title)
            return ToolResult(
                False,
                f"Windows could not bring '{prepared_value.title}' to the foreground.",
                self.risk_level,
            )

        if not focused:
            return ToolResult(
                False,
                f"Windows could not bring '{prepared_value.title}' to the foreground.",
                self.risk_level,
            )

        return ToolResult(
            True,
            f"Switched to {prepared_value.title}.",
            self.risk_level,
            {"title": prepared_value.title, "executable": prepared_value.executable_name},
        )
