from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool, PreparedAppClose
from desktop_assistant.config import AppCatalog, AppDefinition, LaunchTarget
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolArguments, ToolResult
from desktop_assistant.process_control import (
    ProcessControlError,
    WindowsAppProcessController,
    _ProcessEntry,
    _WindowEntry,
)

from conftest import FakeLauncher, FakeProcessController, make_registry


class FakeProcessApi:
    def __init__(
        self,
        *,
        processes: tuple[_ProcessEntry, ...] = (),
        windows: tuple[_WindowEntry, ...] = (),
        fail_processes: bool = False,
        fail_windows: bool = False,
    ) -> None:
        self.process_entries = processes
        self.window_entries = windows
        self.fail_processes = fail_processes
        self.fail_windows = fail_windows
        self.closed_handles: list[int] = []

    def processes(self) -> tuple[_ProcessEntry, ...]:
        if self.fail_processes:
            raise OSError("private process detail")
        return self.process_entries

    def top_level_windows(self) -> tuple[_WindowEntry, ...]:
        if self.fail_windows:
            raise OSError("private window detail")
        return self.window_entries

    def post_close(self, window_handle: int) -> bool:
        self.closed_handles.append(window_handle)
        return True


def test_app_status_reports_allowlisted_running_and_stopped_apps_without_pid() -> None:
    controller = FakeProcessController({"Spotify"})
    registry = make_registry(FakeLauncher(), process_controller=controller)

    running = registry.execute("app_status", {"app_name": "spotify"})
    stopped = registry.execute("app_status", {"app_name": "google chrome"})

    assert isinstance(running, ToolResult) and running.success
    assert running.message == "Spotify is running."
    assert running.details == {"app": "Spotify", "running": True}
    assert isinstance(stopped, ToolResult) and stopped.success
    assert stopped.message == "Chrome is not running."
    assert "pid" not in repr(running.details).casefold()
    assert not registry.has_pending_confirmation()


def test_app_status_rejects_unknown_app_before_process_access() -> None:
    controller = FakeProcessController()
    registry = make_registry(FakeLauncher(), process_controller=controller)

    result = registry.execute("app_status", {"app_name": "malware.exe"})

    assert isinstance(result, ToolResult) and not result.success
    assert controller.status_calls == []


def test_close_app_requires_exact_action_confirmation_and_is_single_use() -> None:
    controller = FakeProcessController({"Notepad"})
    registry = make_registry(FakeLauncher(), process_controller=controller)

    request = registry.execute("close_app", {"app_name": "notepad"})

    assert isinstance(request, ConfirmationRequest)
    assert request.risk_level is RiskLevel.SENSITIVE
    assert request.summary == "Close all open windows of:\nNotepad"
    assert "unsaved work" in request.warning.casefold()
    assert controller.close_calls == []
    first = registry.confirm(request.confirmation_id)
    second = registry.confirm(request.confirmation_id)
    assert first.success
    assert first.message == "Requested all open Notepad windows to close."
    assert [app.display_name for app in controller.close_calls] == ["Notepad"]
    assert not second.success


def test_close_app_cancel_sends_no_close_request() -> None:
    controller = FakeProcessController({"Chrome"})
    registry = make_registry(FakeLauncher(), process_controller=controller)
    request = registry.execute("close_app", {"app_name": "chrome"})
    assert isinstance(request, ConfirmationRequest)

    result = registry.cancel(request.confirmation_id)

    assert result.success
    assert controller.close_calls == []


@pytest.mark.parametrize("app_name", ("unknown", "File Explorer", "explorer"))
def test_close_app_rejects_unknown_or_non_closable_apps(app_name: str) -> None:
    controller = FakeProcessController({"File Explorer"})
    registry = make_registry(FakeLauncher(), process_controller=controller)

    result = registry.execute("close_app", {"app_name": app_name})

    assert isinstance(result, ToolResult) and not result.success
    assert controller.close_calls == []
    assert not registry.has_pending_confirmation()


@pytest.mark.parametrize(
    "arguments",
    (
        {"app_name": "Notepad", "pid": "123"},
        {"app_name": "Notepad", "process_name": "other.exe"},
        {"app_name": "Notepad", "force": "true"},
    ),
)
def test_close_app_rejects_model_supplied_process_control_arguments(arguments: dict[str, str]) -> None:
    registry = make_registry(FakeLauncher())

    result = registry.execute("close_app", arguments)

    assert isinstance(result, ToolResult) and not result.success
    assert not registry.has_pending_confirmation()


def test_close_app_reports_app_no_longer_running_after_confirmation() -> None:
    controller = FakeProcessController()
    registry = make_registry(FakeLauncher(), process_controller=controller)
    request = registry.execute("close_app", {"app_name": "Spotify"})
    assert isinstance(request, ConfirmationRequest)

    result = registry.confirm(request.confirmation_id)

    assert result.success
    assert result.message == "Spotify is no longer running."


def test_close_payload_is_frozen_and_final_catalog_validation_precedes_close() -> None:
    catalog = AppCatalog()
    controller = FakeProcessController({"Notepad"})
    tool = CloseAppTool(catalog, controller)
    preparation = tool.prepare(ToolArguments((("app_name", "Notepad"),)))
    assert not isinstance(preparation, ToolResult)
    payload = preparation.execution_value
    assert isinstance(payload, PreparedAppClose)
    with pytest.raises(FrozenInstanceError):
        payload.app = catalog.resolve("Chrome")  # type: ignore[misc]

    replacement = AppDefinition(
        "Notepad",
        ("notepad",),
        (LaunchTarget("executable", "notepad.exe"),),
        ("different.exe",),
        True,
    )
    catalog._by_alias["notepad"] = replacement  # type: ignore[attr-defined]
    result = tool.execute(payload)
    assert not result.success
    assert controller.close_calls == []


def test_process_matching_is_case_insensitive_and_ignores_untrusted_names() -> None:
    api = FakeProcessApi(
        processes=(
            _ProcessEntry(10, "SPOTIFY.EXE"),
            _ProcessEntry(20, "unrelated.exe"),
        ),
        windows=(
            _WindowEntry(100, 10),
            _WindowEntry(200, 20),
        ),
    )
    app = AppCatalog().resolve("Spotify")
    assert app is not None
    controller = WindowsAppProcessController(api)

    result = controller.request_close(app)

    assert result.was_running
    assert result.requested_window_count == 1
    assert api.closed_handles == [100]


def test_process_controller_intentionally_closes_all_matching_top_level_windows() -> None:
    api = FakeProcessApi(
        processes=(_ProcessEntry(10, "code.exe"),),
        windows=(
            _WindowEntry(101, 10),
            _WindowEntry(102, 10),
            _WindowEntry(201, 99),
        ),
    )
    app = AppCatalog().resolve("VS Code")
    assert app is not None

    result = WindowsAppProcessController(api).request_close(app)

    assert result.requested_window_count == 2
    assert api.closed_handles == [101, 102]


def test_process_controller_handles_zero_windows_and_api_failure() -> None:
    app = AppCatalog().resolve("Chrome")
    assert app is not None
    no_windows = WindowsAppProcessController(
        FakeProcessApi(processes=(_ProcessEntry(10, "chrome.exe"),))
    ).request_close(app)
    assert no_windows.was_running and no_windows.requested_window_count == 0

    with pytest.raises(ProcessControlError):
        WindowsAppProcessController(FakeProcessApi(fail_processes=True)).is_running(app)
    with pytest.raises(ProcessControlError):
        WindowsAppProcessController(
            FakeProcessApi(
                processes=(_ProcessEntry(10, "chrome.exe"),),
                fail_windows=True,
            )
        ).request_close(app)


def test_app_tool_schemas_expose_only_app_name_and_expected_risks() -> None:
    registry = make_registry(FakeLauncher())
    schemas = {schema["name"]: schema for schema in registry.schemas()}

    assert schemas["app_status"]["parameters"] == {
        "type": "object",
        "properties": {
            "app_name": {"type": "string", "description": "Allowlisted application name."}
        },
        "required": ["app_name"],
        "additionalProperties": False,
    }
    assert schemas["close_app"]["parameters"] == schemas["app_status"]["parameters"]
    assert "pid" not in repr(schemas).casefold()
    assert "process_name" not in repr(schemas).casefold()


def test_app_status_process_failure_is_safe() -> None:
    class FailingController(FakeProcessController):
        def is_running(self, app: AppDefinition) -> bool:
            raise ProcessControlError("private detail")

    result = AppStatusTool(AppCatalog(), FailingController()).prepare(
        ToolArguments((("app_name", "Chrome"),))
    )
    assert not isinstance(result, ToolResult)
    executed = AppStatusTool(AppCatalog(), FailingController()).execute(result.execution_value)
    assert not executed.success
    assert "private detail" not in executed.message


@pytest.mark.skipif(os.name != "nt", reason="Windows Toolhelp integration")
def test_windows_controller_can_query_allowlisted_status_without_closing_anything() -> None:
    app = AppCatalog().resolve("Notepad")
    assert app is not None

    running = WindowsAppProcessController().is_running(app)

    assert isinstance(running, bool)
