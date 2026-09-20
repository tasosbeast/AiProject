from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from desktop_assistant.confirmation import ConfirmationManager, ConfirmationOutcome, PreparedAction
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.models import (
    ConfirmationRequest,
    PlanContext,
    RiskLevel,
    ToolArguments,
    ToolPreparation,
    ToolResult,
)
from desktop_assistant.safety import AuthorizationDecision, SafetyPolicy


logger = logging.getLogger(__name__)


class RegisteredTool(Protocol):
    name: str

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult: ...

    def execute(self, prepared_value: object) -> ToolResult: ...


class ToolArgumentType(str, Enum):
    STRING = "string"

    @property
    def python_type(self) -> type[object]:
        return str


@dataclass(frozen=True, slots=True)
class ToolArgumentDefinition:
    name: str
    description: str
    argument_type: ToolArgumentType = ToolArgumentType.STRING
    required: bool = True
    resolve_known_folder: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": self.argument_type.value, "description": self.description}


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    arguments: tuple[ToolArgumentDefinition, ...]
    implementation: RegisteredTool
    risk_level: RiskLevel
    confirmation_summary: Callable[[ToolArguments], str]
    confirmation_warning: str | None = None

    def __post_init__(self) -> None:
        names = tuple(argument.name for argument in self.arguments)
        if not names or len(names) != len(set(names)):
            raise ValueError("Tool argument names must be present and unique.")

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {argument.name: argument.schema() for argument in self.arguments},
            "required": [argument.name for argument in self.arguments if argument.required],
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
    """Authoritative schema validation, preparation, authorization, and execution boundary."""

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
        validated = self._validate_arguments(definition, arguments)
        if isinstance(validated, ToolResult):
            return validated

        try:
            preparation = definition.implementation.prepare(validated)
        except Exception:
            logger.exception("Tool preparation failed", extra={"tool_name": tool_name})
            return self._rejected("That action could not be prepared safely.")
        if isinstance(preparation, ToolResult):
            return preparation
        if not self._valid_preparation(preparation, definition):
            return self._rejected("That action could not be prepared safely.")

        try:
            summary = definition.confirmation_summary(preparation.normalized_arguments).strip()
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

    def dispatch_prepared(
        self,
        prepared: PreparedAction,
        plan_context: PlanContext | None = None,
    ) -> RegistryOutcome:
        if not isinstance(prepared, PreparedAction) or prepared.registry_token is not self._registry_token:
            return self._rejected("The prepared action is invalid.")
        definition = self._definitions.get(prepared.tool_name)
        if definition is None or definition.risk_level is not prepared.risk_level:
            return self._rejected("The prepared action is invalid.")
        if not self._is_deeply_immutable(prepared.execution_value):
            return self._rejected("The prepared action is invalid.")
        decision = self._safety_policy.evaluate(prepared.risk_level)
        if decision is AuthorizationDecision.ALLOW:
            return self._execute_prepared(prepared)
        if decision is AuthorizationDecision.REQUIRE_CONFIRMATION:
            request = self._confirmations.request(prepared, plan_context=plan_context)
            if request is not None:
                return request
            return self._rejected("A confirmation is already pending.")
        return self._rejected("That action is not permitted by the safety policy.")

    def execute(self, tool_name: str, arguments: object) -> RegistryOutcome:
        if self._confirmations.has_pending():
            return self._rejected("Confirm or cancel the pending action before starting another request.")
        prepared = self.prepare(tool_name, arguments)
        if isinstance(prepared, ToolResult):
            return prepared
        return self.dispatch_prepared(prepared)

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
        if not self._is_deeply_immutable(action.execution_value):
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

    def has_pending_confirmation(self, confirmation_id: str | None = None) -> bool:
        return self._confirmations.is_active(confirmation_id)

    def sync_confirmation(self, confirmation_id: str | None = None) -> bool:
        """Query and expire any pending confirmation state. Return True if still active."""
        return self._confirmations.is_active(confirmation_id)

    def discard_pending_confirmation(self) -> None:
        self._confirmations.discard()

    def _validate_arguments(
        self,
        definition: ToolDefinition,
        arguments: object,
    ) -> ToolArguments | ToolResult:
        if not isinstance(arguments, Mapping):
            return self._rejected("The requested action contained invalid arguments.")
        supplied = set(arguments)
        allowed = {argument.name for argument in definition.arguments}
        required = {argument.name for argument in definition.arguments if argument.required}
        if not required.issubset(supplied) or not supplied.issubset(allowed):
            return self._rejected("The requested action contained invalid arguments.")

        values: list[tuple[str, str]] = []
        for argument in definition.arguments:
            if argument.name not in arguments:
                continue
            value = arguments[argument.name]
            if not isinstance(value, argument.argument_type.python_type):
                return self._rejected("The requested action contained invalid arguments.")
            if argument.resolve_known_folder:
                value = self._known_folders.resolve(value)
            values.append((argument.name, value))
        return ToolArguments(tuple(values))

    @classmethod
    def _valid_preparation(cls, preparation: object, definition: ToolDefinition) -> bool:
        if not isinstance(preparation, ToolPreparation):
            return False
        expected = {argument.name for argument in definition.arguments if argument.required}
        supplied = set(preparation.normalized_arguments)
        return (
            expected.issubset(supplied)
            and supplied.issubset({argument.name for argument in definition.arguments})
            and cls._is_deeply_immutable(preparation.execution_value)
        )

    @classmethod
    def _is_deeply_immutable(cls, value: object) -> bool:
        if value is None or isinstance(value, (str, bytes, int, float, bool, Path, Enum)):
            return True
        if isinstance(value, tuple):
            return all(cls._is_deeply_immutable(item) for item in value)
        if isinstance(value, frozenset):
            return all(cls._is_deeply_immutable(item) for item in value)
        if is_dataclass(value) and getattr(type(value), "__dataclass_params__").frozen:
            return all(cls._is_deeply_immutable(getattr(value, item.name)) for item in fields(value))
        return False

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


def string_argument(
    name: str,
    description: str,
    *,
    resolve_known_folder: bool = False,
) -> ToolArgumentDefinition:
    return ToolArgumentDefinition(name, description, resolve_known_folder=resolve_known_folder)


def default_tool_definitions(
    open_app: RegisteredTool,
    open_folder: RegisteredTool,
    open_website: RegisteredTool,
    *additional_tools: RegisteredTool,
) -> tuple[ToolDefinition, ...]:
    definitions = [
        ToolDefinition(
            "open_app",
            "Open one allowlisted Windows application by its friendly name.",
            (string_argument("app_name", "Allowlisted application name."),),
            open_app,
            RiskLevel.SAFE,
            lambda arguments: f"Open application: {arguments['app_name']}",
        ),
        ToolDefinition(
            "open_folder",
            "Open one existing local folder.",
            (
                string_argument(
                    "path",
                    "Existing folder path or known-folder name.",
                    resolve_known_folder=True,
                ),
            ),
            open_folder,
            RiskLevel.SAFE,
            lambda arguments: f"Open folder: {arguments['path']}",
        ),
        ToolDefinition(
            "open_website",
            "Open one website using an http or https URL.",
            (string_argument("url", "A complete http or https URL."),),
            open_website,
            RiskLevel.SAFE,
            lambda arguments: f"Open website: {arguments['url']}",
        ),
    ]
    definitions.extend(_additional_tool_definition(tool) for tool in additional_tools)
    return tuple(definitions)


def _additional_tool_definition(tool: RegisteredTool) -> ToolDefinition:
    app_name = string_argument("app_name", "Allowlisted application name.")
    if tool.name == "app_status":
        return ToolDefinition(
            tool.name,
            "Check whether one allowlisted Windows application is currently running.",
            (app_name,),
            tool,
            RiskLevel.SAFE,
            lambda arguments: f"Check application status: {arguments['app_name']}",
        )
    if tool.name == "close_app":
        return ToolDefinition(
            tool.name,
            "Request a normal close for all open windows of one closable allowlisted application.",
            (app_name,),
            tool,
            RiskLevel.SENSITIVE,
            lambda arguments: f"Close all open windows of:\n{arguments['app_name']}",
            (
                "The application may contain unsaved work. It will receive a normal "
                "close request and may ask you to save changes."
            ),
        )
    path = string_argument(
        "path",
        "Local filesystem path or known-folder path.",
        resolve_known_folder=True,
    )
    source = string_argument(
        "source",
        "Exact existing local source path.",
        resolve_known_folder=True,
    )
    destination = string_argument(
        "destination",
        "Exact new local destination path.",
        resolve_known_folder=True,
    )
    if tool.name == "list_folder":
        return ToolDefinition(
            tool.name,
            "List up to 100 direct entries in one existing local folder without recursion.",
            (path,),
            tool,
            RiskLevel.SAFE,
            lambda arguments: f"List folder: {arguments['path']}",
        )
    if tool.name == "path_exists":
        return ToolDefinition(
            tool.name,
            "Check whether one local path exists and whether it is a file or folder.",
            (path,),
            tool,
            RiskLevel.SAFE,
            lambda arguments: f"Check path: {arguments['path']}",
        )
    if tool.name == "create_folder":
        return ToolDefinition(
            tool.name,
            "Create exactly one local folder whose parent already exists.",
            (path,),
            tool,
            RiskLevel.SENSITIVE,
            lambda arguments: f"Create folder:\n{arguments['path']}",
        )
    if tool.name == "rename_path":
        return ToolDefinition(
            tool.name,
            "Rename one existing local file or folder within its current parent directory.",
            (source, destination),
            tool,
            RiskLevel.SENSITIVE,
            lambda arguments: (
                f"Rename:\n{arguments['source']}\n→\n{arguments['destination']}"
            ),
        )
    if tool.name == "move_path":
        return ToolDefinition(
            tool.name,
            "Move one local file or folder to an exact destination on the same volume.",
            (source, destination),
            tool,
            RiskLevel.SENSITIVE,
            lambda arguments: (
                f"Move:\n{arguments['source']}\n→\n{arguments['destination']}"
            ),
        )
    raise ValueError(f"Unknown additional tool definition: {tool.name}")
