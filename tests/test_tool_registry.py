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
        "system_status",
        "open_project",
        "run_project_task",
        "window_info",
        "focus_window",
        "window_input",
        "ui_inspect",
        "ui_action",
        "visual_inspect",
        "visual_target",
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

    status = next(schema for schema in schemas if schema["name"] == "system_status")
    assert status["parameters"]["required"] == ["metric"]
    assert status["parameters"]["properties"] == {
        "metric": {
            "type": "string",
            "description": "System metric to check.",
            "enum": ["cpu", "memory", "battery", "disk", "overview"],
        },
    }

    open_proj = next(schema for schema in schemas if schema["name"] == "open_project")
    assert open_proj["parameters"]["required"] == ["project_name"]
    assert open_proj["parameters"]["properties"] == {
        "project_name": {
            "type": "string",
            "description": "Name of the trusted project.",
            "enum": ["AiProject"],
        },
    }

    run_task = next(schema for schema in schemas if schema["name"] == "run_project_task")
    assert run_task["parameters"]["required"] == ["project_name", "task"]
    assert run_task["parameters"]["properties"] == {
        "project_name": {
            "type": "string",
            "description": "Name of the trusted project.",
            "enum": ["AiProject"],
        },
        "task": {
            "type": "string",
            "description": "Predefined task to run.",
            "enum": ["tests"],
        },
    }

    win_info = next(schema for schema in schemas if schema["name"] == "window_info")
    assert win_info["parameters"]["required"] == ["action"]
    assert win_info["parameters"]["properties"] == {
        "action": {
            "type": "string",
            "description": "Action to perform ('list' or 'active').",
            "enum": ["list", "active"],
        },
    }

    focus = next(schema for schema in schemas if schema["name"] == "focus_window")
    assert focus["parameters"]["required"] == ["query"]
    assert focus["parameters"]["properties"] == {
        "query": {
            "type": "string",
            "description": "Title, name, or application of the window to focus.",
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

    ui_act = next(schema for schema in schemas if schema["name"] == "ui_action")
    assert ui_act["parameters"]["required"] == ["query", "control", "action"]
    assert ui_act["parameters"]["properties"] == {
        "query": {
            "type": "string",
            "description": "Existing window title or application name.",
        },
        "control": {
            "type": "string",
            "description": "Exact accessible control name or label.",
        },
        "action": {
            "type": "string",
            "description": "Exact UI action to perform.",
            "enum": ["invoke", "select", "expand", "collapse", "toggle_on", "toggle_off"],
        },
    }

    vis_insp = next(schema for schema in schemas if schema["name"] == "visual_inspect")
    assert vis_insp["parameters"]["required"] == ["query"]
    assert vis_insp["parameters"]["properties"] == {
        "query": {
            "type": "string",
            "description": "Explicit existing window title or application name.",
        },
        "goal": {
            "type": "string",
            "description": "Short visual question or description goal.",
        },
    }

    vis_tgt = next(schema for schema in schemas if schema["name"] == "visual_target")
    assert vis_tgt["parameters"]["required"] == ["query", "target"]
    assert vis_tgt["parameters"]["properties"] == {
        "query": {
            "type": "string",
            "description": "Explicit existing window title or application name.",
        },
        "target": {
            "type": "string",
            "description": "Short description of the visible element to locate.",
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
        registry.execute("system_status", {"metric": "temperature"}),
        registry.execute("system_status", {"metric": 99}),
        registry.execute("system_status", {}),
        registry.execute("open_project", {"project_name": "MaliciousProject"}),
        registry.execute("open_project", {"project_name": 123}),
        registry.execute("open_project", {}),
        registry.execute("run_project_task", {"project_name": "AiProject", "task": "format"}),
        registry.execute("run_project_task", {"project_name": "Other", "task": "tests"}),
        registry.execute("run_project_task", {"project_name": "AiProject"}),
        registry.execute("run_project_task", {}),
        registry.execute("window_info", {"action": "minimize"}),
        registry.execute("window_info", {"action": 123}),
        registry.execute("window_info", {}),
        registry.execute("focus_window", {"query": 123}),
        registry.execute("focus_window", {}),
        registry.execute("focus_window", {"handle": 12345}),
        registry.execute("focus_window", {"pid": 54321}),
        registry.execute("ui_action", {"query": "Notepad", "control": "Settings", "action": "click"}),
        registry.execute("ui_action", {"query": "Notepad", "control": "Settings", "action": 123}),
        registry.execute("ui_action", {"query": "Notepad", "action": "invoke"}),
        registry.execute("ui_action", {"control": "Settings", "action": "invoke"}),
        registry.execute("ui_action", {}),
        registry.execute("ui_action", {"query": "Notepad", "control": "Settings", "action": "invoke", "runtime_id": [1, 2]}),
    )

    assert all(not result.success for result in results)
    assert launcher.apps == []


def test_production_schemas_have_no_delete_or_shell_capability() -> None:
    schemas = make_registry(FakeLauncher()).schemas()
    names = {schema["name"] for schema in schemas}

    assert not names & {
        "delete_file",
        "delete_folder",
        "remove_path",
        "run_shell",
        "powershell",
        "cmd",
    }
    assert not any("handle" in s["parameters"].get("properties", {}) for s in schemas)
    assert not any("pid" in s["parameters"].get("properties", {}) for s in schemas)
    assert not any("hwnd" in s["parameters"].get("properties", {}) for s in schemas)
    assert not any("runtime_id" in s["parameters"].get("properties", {}) for s in schemas)


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
