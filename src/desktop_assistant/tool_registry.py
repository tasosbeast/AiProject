from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, TypeAlias

from desktop_assistant.confirmation import (
    ConfirmationManager,
    ConfirmationOutcome,
    PreparedAction,
)
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolPreparation, ToolResult
from desktop_assistant.safety import AuthorizationDecision, SafetyPolicy


logger = logging.getLogger(__name__)


class RegisteredTool(Protocol):
    name: str

    def prepare(self, value: str) -> ToolPreparation | ToolResult: ...

    def execute(self, prepared_value: object) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    argument_name: str
    argument_description: str
    implementation: RegisteredTool
    risk_level: RiskLevel
    confirmation_summary: Callable[[Mapping[str, str]], str]
    confirmation_warning: str | None = None

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                self.argument_name: {
                    "type": "string",
                    "description": self.argument_description,
                }
            },
            "required": [self.argument_name],
            "additionalProperties": False,
        }

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema(),
            "strict": True,
        }


RegistryOutcome: TypeAlias = ToolResult | ConfirmationRequest


class ToolRegistry:
    """Authoritative preparation, authorization, and execution boundary."""

    def __init__(
        self,
        definitions: tuple[ToolDefinition, ...],
        safety_policy: SafetyPolicy | None = None,
        known_folders: KnownFolderResolver | None = None,
        confirmation_manager: ConfirmationManager | None = None,
    ) -> None:
        self._definitions = {definition.name: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("Tool names must be unique.")
        if any(not isinstance(definition.risk_level, RiskLevel) for definition in definitions):
            raise ValueError("Every tool definition must declare a valid risk level.")
        self._safety_policy = safety_policy or SafetyPolicy()
        self._known_folders = known_folders or KnownFolderResolver()
        self._confirmations = confirmation_manager or ConfirmationManager()
        self._registry_token = object()

    def schemas(self) -> list[dict[str, Any]]:
        return [deepcopy(definition.openai_schema()) for definition in self._definitions.values()]

    def prepare(self, tool_name: str, arguments: object) -> PreparedAction | ToolResult:
        definition = self._definitions.get(tool_name)
        if definition is None:
            return self._rejected("That action is not available.")
        if not isinstance(arguments, Mapping):
            return self._rejected("The requested action contained invalid arguments.")
        required_keys = {definition.argument_name}
        if set(arguments) != required_keys:
            return self._rejected("The requested action contained invalid arguments.")
        value = arguments.get(definition.argument_name)
        if not isinstance(value, str):
            return self._rejected("The requested action contained invalid arguments.")
        if tool_name == "open_folder":
            value = self._known_folders.resolve(value)

        try:
            preparation = definition.implementation.prepare(value)
        except Exception:
            logger.exception("Tool preparation failed", extra={"tool_name": tool_name})
            return self._rejected("That action could not be prepared safely.")
        if isinstance(preparation, ToolResult):
            return preparation
        if not self._valid_preparation(preparation, definition.argument_name):
            return self._rejected("That action could not be prepared safely.")

        normalized_arguments = dict(preparation.normalized_arguments)
        try:
            summary = definition.confirmation_summary(normalized_arguments).strip()
        except Exception:
            logger.exception("Confirmation summary generation failed", extra={"tool_name": tool_name})
            return self._rejected("That action could not be prepared safely.")
        if not summary:
            return self._rejected("That action could not be prepared safely.")
        warning = definition.confirmation_warning or self._default_warning(definition.risk_level)
        return PreparedAction(
            tool_name=tool_name,
            normalized_arguments=preparation.normalized_arguments,
            risk_level=definition.risk_level,
            summary=summary,
            warning=warning,
            execution_value=preparation.execution_value,
            executor=definition.implementation.execute,
            registry_token=self._registry_token,
        )

    def execute(self, tool_name: str, arguments: object) -> RegistryOutcome:
        if self._confirmations.has_pending():
            return self._rejected("Confirm or cancel the pending action before starting another request.")
        prepared = self.prepare(tool_name, arguments)
        if isinstance(prepared, ToolResult):
            return prepared
        decision = self._safety_policy.evaluate(prepared.risk_level)
        if decision is AuthorizationDecision.ALLOW:
            return self._execute_prepared(prepared)
        if decision is AuthorizationDecision.REQUIRE_CONFIRMATION:
            request = self._confirmations.request(prepared)
            if request is not None:
                return request
            return self._rejected("A confirmation is already pending.")
        return self._rejected("That action is not permitted by the safety policy.")

    def confirm(self, confirmation_id: str) -> ToolResult:
        resolution = self._confirmations.approve(confirmation_id)
        if resolution.outcome is ConfirmationOutcome.EXPIRED:
            return self._rejected("That confirmation expired. Please request the action again.")
        if resolution.outcome is not ConfirmationOutcome.APPROVED or resolution.action is None:
            return self._rejected("That confirmation is no longer valid.")
        result = self._execute_prepared(resolution.action)
        logger.info(
            "Confirmed action completed",
            extra={
                "tool_name": resolution.action.tool_name,
                "risk_level": resolution.action.risk_level.value,
                "success": result.success,
            },
        )
        return result

    def cancel(self, confirmation_id: str) -> ToolResult:
        resolution = self._confirmations.cancel(confirmation_id)
        if resolution.outcome is ConfirmationOutcome.CANCELLED:
            return ToolResult(True, "Action cancelled.", RiskLevel.SAFE)
        if resolution.outcome is ConfirmationOutcome.EXPIRED:
            return self._rejected("That confirmation expired. The action was discarded.")
        return self._rejected("That confirmation is no longer valid.")

    def _execute_prepared(self, action: PreparedAction) -> ToolResult:
        if not isinstance(action, PreparedAction) or action.registry_token is not self._registry_token:
            return self._rejected("The prepared action is invalid.")
        definition = self._definitions.get(action.tool_name)
        if definition is None or definition.risk_level is not action.risk_level:
            return self._rejected("The prepared action is invalid.")
        try:
            result = action.executor(action.execution_value)
        except Exception:
            logger.exception(
                "Prepared action execution failed",
                extra={"tool_name": action.tool_name, "risk_level": action.risk_level.value},
            )
            return ToolResult(False, "The confirmed action could not be completed.", action.risk_level)
        if not isinstance(result, ToolResult):
            return ToolResult(False, "The confirmed action returned an invalid result.", action.risk_level)
        return result

    def has_pending_confirmation(self) -> bool:
        return self._confirmations.has_pending()

    def discard_pending_confirmation(self) -> None:
        self._confirmations.discard()

    @staticmethod
    def _valid_preparation(preparation: object, argument_name: str) -> bool:
        if not isinstance(preparation, ToolPreparation):
            return False
        arguments = preparation.normalized_arguments
        return (
            len(arguments) == 1
            and arguments[0][0] == argument_name
            and isinstance(arguments[0][1], str)
        )

    @staticmethod
    def _default_warning(risk_level: RiskLevel) -> str:
        if risk_level is RiskLevel.DESTRUCTIVE:
            return "This action may permanently change or remove data. Review it carefully."
        if risk_level is RiskLevel.SENSITIVE:
            return "Review this action carefully before allowing it."
        return ""

    @staticmethod
    def _rejected(message: str) -> ToolResult:
        return ToolResult(False, message, RiskLevel.SAFE)


def default_tool_definitions(
    open_app: RegisteredTool,
    open_folder: RegisteredTool,
    open_website: RegisteredTool,
) -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="open_app",
            description=(
                "Open one allowlisted Windows application by its friendly name. "
                "Never provide executable paths, arguments, or shell commands."
            ),
            argument_name="app_name",
            argument_description="Allowlisted application name, such as Spotify or Chrome.",
            implementation=open_app,
            risk_level=RiskLevel.SAFE,
            confirmation_summary=lambda arguments: f"Open application: {arguments['app_name']}",
        ),
        ToolDefinition(
            name="open_folder",
            description=(
                "Open one existing folder. Use a full user-provided path or one known-folder "
                "name: Home, Desktop, Documents, Downloads, Music, Pictures, or Videos."
            ),
            argument_name="path",
            argument_description="Existing folder path or an explicitly supported known-folder name.",
            implementation=open_folder,
            risk_level=RiskLevel.SAFE,
            confirmation_summary=lambda arguments: f"Open folder: {arguments['path']}",
        ),
        ToolDefinition(
            name="open_website",
            description="Open one website using an http or https URL.",
            argument_name="url",
            argument_description="A complete http or https URL. Add https:// to bare domain names.",
            implementation=open_website,
            risk_level=RiskLevel.SAFE,
            confirmation_summary=lambda arguments: f"Open website: {arguments['url']}",
        ),
    )
