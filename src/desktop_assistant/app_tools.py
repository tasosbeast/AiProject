from __future__ import annotations

import logging
from dataclasses import dataclass

from desktop_assistant.config import AppCatalog, AppDefinition
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult
from desktop_assistant.process_control import AppProcessController, ProcessControlError


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedAppClose:
    app: AppDefinition


class AppStatusTool:
    name = "app_status"
    risk_level = RiskLevel.SAFE

    def __init__(self, catalog: AppCatalog, controller: AppProcessController) -> None:
        self._catalog = catalog
        self._controller = controller

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        app = self._resolve(arguments["app_name"])
        if isinstance(app, ToolResult):
            return app
        return ToolPreparation(app, ToolArguments((("app_name", app.display_name),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, AppDefinition) or not self._is_current(prepared_value):
            return self._rejected("The prepared application status check is no longer valid.")
        try:
            running = self._controller.is_running(prepared_value)
        except ProcessControlError:
            logger.warning(
                "Application status check failed",
                extra={"app_name": prepared_value.display_name, "tool_name": self.name},
            )
            return self._rejected("Windows could not check the application status.")
        logger.info(
            "Application status checked",
            extra={
                "app_name": prepared_value.display_name,
                "tool_name": self.name,
                "success": True,
            },
        )
        state = "running" if running else "not running"
        return ToolResult(
            True,
            f"{prepared_value.display_name} is {state}.",
            self.risk_level,
            {"app": prepared_value.display_name, "running": running},
        )

    def _resolve(self, app_name: str) -> AppDefinition | ToolResult:
        app = self._catalog.resolve(app_name)
        if app is None or not app.process_names:
            return self._rejected("That application is not available for status checks.")
        return app

    def _is_current(self, app: AppDefinition) -> bool:
        return bool(app.process_names) and self._catalog.resolve(app.display_name) == app

    def _rejected(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)


class CloseAppTool:
    name = "close_app"
    risk_level = RiskLevel.SENSITIVE

    def __init__(self, catalog: AppCatalog, controller: AppProcessController) -> None:
        self._catalog = catalog
        self._controller = controller

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        app = self._catalog.resolve(arguments["app_name"])
        if app is None:
            return self._rejected("That application is not available for closing.")
        if not app.can_close or not app.process_names:
            return self._rejected(f"{app.display_name} cannot be closed by the assistant.")
        return ToolPreparation(
            PreparedAppClose(app),
            ToolArguments((("app_name", app.display_name),)),
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedAppClose):
            return self._invalid_prepared()
        app = prepared_value.app
        current = self._catalog.resolve(app.display_name)
        if current != app or not current.can_close or not current.process_names:
            return self._invalid_prepared()
        try:
            outcome = self._controller.request_close(current)
        except ProcessControlError:
            logger.warning(
                "Graceful application close request failed",
                extra={"app_name": current.display_name, "tool_name": self.name},
            )
            return self._rejected("Windows could not request the application to close.")
        logger.info(
            "Graceful application close evaluated",
            extra={
                "app_name": current.display_name,
                "tool_name": self.name,
                "success": outcome.requested_window_count > 0 or not outcome.was_running,
            },
        )
        if not outcome.was_running:
            return ToolResult(
                True,
                f"{current.display_name} is no longer running.",
                self.risk_level,
                {"app": current.display_name, "requested_window_count": 0},
            )
        if outcome.requested_window_count == 0:
            return self._rejected(f"No open {current.display_name} windows were found.")
        return ToolResult(
            True,
            f"Requested all open {current.display_name} windows to close.",
            self.risk_level,
            {
                "app": current.display_name,
                "requested_window_count": outcome.requested_window_count,
            },
        )

    def _invalid_prepared(self) -> ToolResult:
        return self._rejected("The prepared application close request is no longer valid.")

    def _rejected(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)
