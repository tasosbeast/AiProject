from __future__ import annotations

from desktop_assistant.models import RiskLevel, ToolResult


class SafetyPolicy:
    """Central execution policy for tool risk levels."""

    def authorize(self, risk_level: RiskLevel) -> ToolResult | None:
        if risk_level is RiskLevel.SAFE:
            return None
        return ToolResult(
            False,
            "This action requires confirmation, which is not available in this version.",
            risk_level,
        )

