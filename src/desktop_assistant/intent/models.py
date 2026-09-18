from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class IntentKind(str, Enum):
    TOOL_ACTION = "tool_action"
    CONVERSATION = "conversation"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ToolAction:
    tool_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IntentResult:
    kind: IntentKind
    action: ToolAction | None = None
    message: str | None = None

    @classmethod
    def tool_action(cls, tool_name: str, arguments: Mapping[str, Any]) -> "IntentResult":
        return cls(IntentKind.TOOL_ACTION, ToolAction(tool_name, dict(arguments)))

    @classmethod
    def conversation(cls, message: str) -> "IntentResult":
        return cls(IntentKind.CONVERSATION, message=message)

    @classmethod
    def unsupported(cls, message: str) -> "IntentResult":
        return cls(IntentKind.UNSUPPORTED, message=message)
