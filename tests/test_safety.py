from desktop_assistant.models import RiskLevel
from desktop_assistant.safety import SafetyPolicy


def test_safe_action_is_authorized() -> None:
    assert SafetyPolicy().authorize(RiskLevel.SAFE) is None


def test_sensitive_action_is_denied_until_confirmation_exists() -> None:
    result = SafetyPolicy().authorize(RiskLevel.SENSITIVE)

    assert result is not None
    assert not result.success
    assert "requires confirmation" in result.message

