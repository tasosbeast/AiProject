from desktop_assistant.models import RiskLevel
from desktop_assistant.safety import AuthorizationDecision, SafetyPolicy


def test_safe_action_is_authorized() -> None:
    assert SafetyPolicy().evaluate(RiskLevel.SAFE) is AuthorizationDecision.ALLOW


def test_sensitive_action_is_denied_until_confirmation_exists() -> None:
    assert (
        SafetyPolicy().evaluate(RiskLevel.SENSITIVE)
        is AuthorizationDecision.REQUIRE_CONFIRMATION
    )


def test_unknown_risk_fails_closed() -> None:
    assert SafetyPolicy().evaluate("new-risk") is AuthorizationDecision.DENY
