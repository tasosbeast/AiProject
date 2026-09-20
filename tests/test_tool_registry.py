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
        "app_status",
        "close_app",
        "list_folder",
        "path_exists",
        "create_folder",
        "rename_path",
        "move_path",
        "volume_control",
        "media_control",
    }
    assert all(schema["strict"] is True for schema in schemas)
    assert all(schema["parameters"]["additionalProperties"] is False for schema in schemas)

    volume = next(schema for schema in schemas if schema["name"] == "volume_control")
    assert volume["parameters"]["required"] == ["action"]
    assert volume["parameters"]["properties"] == {
        "action": {
            "type": "string",
            "description": "Volume action to perform.",
            "enum": ["volume_up", "volume_down", "mute_toggle"],
        },
    }

    media = next(schema for schema in schemas if schema["name"] == "media_control")
    assert media["parameters"]["required"] == ["action"]
    assert media["parameters"]["properties"] == {
        "action": {
            "type": "string",
            "description": "Media action to perform.",
            "enum": ["play_pause", "next_track", "previous_track"],
        },
    }

    rename = next(schema for schema in schemas if schema["name"] == "rename_path")
    assert rename["parameters"]["required"] == ["source", "destination"]
    assert rename["parameters"]["properties"] == {
        "source": {
            "type": "string",
            "description": "Exact existing local source path.",
        },
        "destination": {
            "type": "string",
            "description": "Exact new local destination path.",
        },
    }


def test_registry_rejects_unknown_tool_and_invalid_arguments() -> None:
    launcher = FakeLauncher()
    registry = make_registry(launcher)

    results = (
        registry.execute("unknown", {"value": "Spotify"}),
        registry.execute("open_app", {}),
        registry.execute("open_app", {"app_name": 7}),
        registry.execute("open_app", {"app_name": "Spotify", "extra": "bad"}),
        registry.execute("rename_path", {"source": "a"}),
        registry.execute("rename_path", {"source": "a", "destination": 4}),
        registry.execute(
            "rename_path",
            {"source": "a", "destination": "b", "risk_level": "safe"},
        ),
        registry.execute(
            "rename_path",
            {"source": "a", "destination": "b", "confirmation_summary": "Allow"},
        ),
        registry.execute("volume_control", {"action": "explode"}),
        registry.execute("volume_control", {"action": 123}),
        registry.execute("media_control", {"action": "fast_forward"}),
        registry.execute("media_control", {}),
    )

    assert all(not result.success for result in results)
    assert launcher.apps == []


def test_production_schemas_have_no_delete_or_shell_capability() -> None:
    names = {schema["name"] for schema in make_registry(FakeLauncher()).schemas()}

    assert not names & {
        "delete_file",
        "delete_folder",
        "remove_path",
        "run_shell",
        "powershell",
        "cmd",
    }


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


def test_registry_resolves_known_folder_prefix_for_nested_paths(tmp_path) -> None:
    downloads = tmp_path / "Downloads"
    nested = downloads / "Manuals"
    nested.mkdir(parents=True)
    registry = make_registry(FakeLauncher(), home=tmp_path)

    result = registry.execute("path_exists", {"path": r"Downloads\Manuals"})

    assert result.success
    assert result.details["is_directory"] is True
