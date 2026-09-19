from __future__ import annotations

from enum import Enum

from desktop_assistant.models import RiskLevel


class AuthorizationDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


class SafetyPolicy:
    """Fail-closed authorization policy for application-owned risk metadata."""

    def evaluate(self, risk_level: object) -> AuthorizationDecision:
        if risk_level is RiskLevel.SAFE:
            return AuthorizationDecision.ALLOW
        if risk_level in {RiskLevel.SENSITIVE, RiskLevel.DESTRUCTIVE}:
            return AuthorizationDecision.REQUIRE_CONFIRMATION
        return AuthorizationDecision.DENY
