from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class IntentKind(str, Enum):
    TOOL_ACTION = "tool_action"
    ACTION_PLAN = "action_plan"
    CONVERSATION = "conversation"
    UNSUPPORTED = "unsupported"
    OBSERVE_UI_THEN_DECIDE = "observe_ui_then_decide"
    ADAPTIVE_UI_TASK = "adaptive_ui_task"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ToolAction:
    tool_name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):
            raise TypeError("ToolAction arguments must be a Mapping.")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ObserveUIIntent:
    query: str

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("Target window query must be a non-empty string.")
        object.__setattr__(self, "query", self.query.strip())


@dataclass(frozen=True, slots=True)
class AdaptiveUITaskIntent:
    goal: str
    query: str

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("Adaptive UI task goal must be a non-empty string.")
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("Target window query must be a non-empty string.")
        object.__setattr__(self, "goal", self.goal.strip())
        object.__setattr__(self, "query", self.query.strip())


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """An ordered, immutable sequence of 2 to 3 computer actions."""

    actions: tuple[ToolAction, ...]

    def __post_init__(self) -> None:
        if not (2 <= len(self.actions) <= 3):
            raise ValueError(f"Action plan must contain 2 or 3 actions, got {len(self.actions)}.")
        if any(not isinstance(action, ToolAction) for action in self.actions):
            raise TypeError("Action plan actions must be ToolAction instances.")


@dataclass(frozen=True, slots=True)
class IntentResult:
    kind: IntentKind
    action: ToolAction | None = None
    plan: ActionPlan | None = None
    message: str | None = None
    observe_ui: ObserveUIIntent | None = None
    adaptive_task: AdaptiveUITaskIntent | None = None

    @property
    def query(self) -> str | None:
        if self.observe_ui is not None:
            return self.observe_ui.query
        if self.adaptive_task is not None:
            return self.adaptive_task.query
        return None

    @classmethod
    def tool_action(cls, tool_name: str, arguments: Mapping[str, Any]) -> "IntentResult":
        return cls(IntentKind.TOOL_ACTION, action=ToolAction(tool_name, dict(arguments)))

    @classmethod
    def action_plan(cls, actions: tuple[ToolAction, ...] | list[ToolAction]) -> "IntentResult":
        return cls(IntentKind.ACTION_PLAN, plan=ActionPlan(tuple(actions)))

    @classmethod
    def conversation(cls, message: str) -> "IntentResult":
        return cls(IntentKind.CONVERSATION, message=message)

    @classmethod
    def unsupported(cls, message: str) -> "IntentResult":
        return cls(IntentKind.UNSUPPORTED, message=message)

    @classmethod
    def observe_ui_then_decide(cls, query: str) -> "IntentResult":
        return cls(IntentKind.OBSERVE_UI_THEN_DECIDE, observe_ui=ObserveUIIntent(query))

    @classmethod
    def adaptive_ui_task(cls, goal: str, query: str) -> "IntentResult":
        return cls(IntentKind.ADAPTIVE_UI_TASK, adaptive_task=AdaptiveUITaskIntent(goal=goal, query=query))

    @classmethod
    def complete(cls, message: str) -> "IntentResult":
        return cls(IntentKind.COMPLETE, message=message)
