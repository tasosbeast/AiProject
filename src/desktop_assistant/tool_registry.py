from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.safety import SafetyPolicy


class RegisteredTool(Protocol):
    name: str
    risk_level: RiskLevel

    def run(self, value: str) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    argument_name: str
    argument_description: str
    implementation: RegisteredTool

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


class ToolRegistry:
    """Authoritative metadata, validation, safety, and execution boundary."""

    def __init__(
        self,
        definitions: tuple[ToolDefinition, ...],
        safety_policy: SafetyPolicy | None = None,
        known_folders: KnownFolderResolver | None = None,
    ) -> None:
        self._definitions = {definition.name: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("Tool names must be unique.")
        self._safety_policy = safety_policy or SafetyPolicy()
        self._known_folders = known_folders or KnownFolderResolver()

    def schemas(self) -> list[dict[str, Any]]:
        return [deepcopy(definition.openai_schema()) for definition in self._definitions.values()]

    def execute(self, tool_name: str, arguments: object) -> ToolResult:
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

        denial = self._safety_policy.authorize(definition.implementation.risk_level)
        if denial is not None:
            return denial

        if tool_name == "open_folder":
            value = self._known_folders.resolve(value)
        return definition.implementation.run(value)

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
        ),
        ToolDefinition(
            name="open_website",
            description="Open one website using an http or https URL.",
            argument_name="url",
            argument_description="A complete http or https URL. Add https:// to bare domain names.",
            implementation=open_website,
        ),
    )
