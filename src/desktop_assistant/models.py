from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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
class ToolPreparation:
    """Side-effect-free, normalized input produced by a trusted tool."""

    execution_value: object = field(repr=False)
    normalized_arguments: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    """The limited, immutable confirmation information exposed to adapters."""

    confirmation_id: str
    summary: str
    risk_level: RiskLevel
    warning: str


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
