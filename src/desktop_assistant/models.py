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

