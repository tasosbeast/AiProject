from __future__ import annotations

from desktop_assistant.models import RiskLevel
from desktop_assistant.safety import AuthorizationDecision

from conftest import FakeLauncher, make_registry


def test_registry_generates_strict_schemas_from_execution_metadata() -> None:
    schemas = make_registry(FakeLauncher()).schemas()

    assert {schema["name"] for schema in schemas} == {
        "open_app",
        "open_folder",
        "open_website",
    }
    assert all(schema["strict"] is True for schema in schemas)
    assert all(schema["parameters"]["additionalProperties"] is False for schema in schemas)


def test_registry_rejects_unknown_tool_and_invalid_arguments() -> None:
    launcher = FakeLauncher()
    registry = make_registry(launcher)

    results = (
        registry.execute("unknown", {"value": "Spotify"}),
        registry.execute("open_app", {}),
        registry.execute("open_app", {"app_name": 7}),
        registry.execute("open_app", {"app_name": "Spotify", "extra": "bad"}),
    )

    assert all(not result.success for result in results)
    assert launcher.apps == []


def test_registry_keeps_safety_policy_in_execution_path() -> None:
    class DenyPolicy:
        def evaluate(self, risk_level: RiskLevel) -> AuthorizationDecision:
            return AuthorizationDecision.DENY

    launcher = FakeLauncher()
    registry = make_registry(launcher, safety_policy=DenyPolicy())  # type: ignore[arg-type]

    result = registry.execute("open_app", {"app_name": "Spotify"})

    assert not result.success
    assert result.message == "That action is not permitted by the safety policy."
    assert launcher.apps == []


def test_registry_resolves_only_explicit_known_folder(tmp_path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    launcher = FakeLauncher()
    registry = make_registry(launcher, home=tmp_path)

    result = registry.execute("open_folder", {"path": "Downloads"})

    assert result.success
    assert launcher.folders == [downloads.resolve()]
