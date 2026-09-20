from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Iterator, Mapping
from typing import Any


class RiskLevel(str, Enum):
    """Risk classification used by tools and future permission policies."""

    SAFE = "safe"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A user-facing result returned by every tool."""

    success: bool
    message: str
    risk_level: RiskLevel
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolArguments(Mapping[str, str]):
    """Ordered, immutable, application-owned tool arguments."""

    values: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        names = tuple(name for name, _value in self.values)
        if len(names) != len(set(names)) or any(not isinstance(value, str) for _, value in self.values):
            raise ValueError("Tool arguments must contain unique string values.")

    def __getitem__(self, key: str) -> str:
        for name, value in self.values:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _value in self.values)

    def __len__(self) -> int:
        return len(self.values)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        order: tuple[str, ...] | None = None,
    ) -> "ToolArguments":
        names = order or tuple(values)
        return cls(tuple((name, values[name]) for name in names))


@dataclass(frozen=True, slots=True)
class ToolPreparation:
    """Side-effect-free, normalized input produced by a trusted tool."""

    execution_value: object = field(repr=False)
    normalized_arguments: ToolArguments


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Context information when a confirmation request is part of a multi-step plan."""

    step_index: int
    total_steps: int
    completed_summaries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    """The limited, immutable confirmation information exposed to adapters."""

    confirmation_id: str
    summary: str
    risk_level: RiskLevel
    warning: str
    plan_context: PlanContext | None = None


class AssistantResponseKind(str, Enum):
    COMPLETED = "completed"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass(frozen=True, slots=True)
class AssistantResponse:
    kind: AssistantResponseKind
    result: ToolResult | None = None
    confirmation: ConfirmationRequest | None = None

    def __post_init__(self) -> None:
        completed = self.kind is AssistantResponseKind.COMPLETED
        if completed != (self.result is not None) or completed == (self.confirmation is not None):
            raise ValueError("AssistantResponse payload does not match its kind.")

    @classmethod
    def completed(cls, result: ToolResult) -> "AssistantResponse":
        return cls(AssistantResponseKind.COMPLETED, result=result)

    @classmethod
    def confirmation_required(cls, request: ConfirmationRequest) -> "AssistantResponse":
        return cls(AssistantResponseKind.CONFIRMATION_REQUIRED, confirmation=request)

    @property
    def success(self) -> bool:
        return bool(self.result and self.result.success)

    @property
    def message(self) -> str:
        return self.result.message if self.result is not None else ""

    @property
    def risk_level(self) -> RiskLevel | None:
        return self.result.risk_level if self.result is not None else None
