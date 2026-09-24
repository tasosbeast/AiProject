from __future__ import annotations

import os
from pathlib import Path
import pytest

from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog, AppDefinition
from desktop_assistant.filesystem_tools import FilesystemPathValidator
from desktop_assistant.intent.models import ActionPlan, IntentKind, IntentResult, ToolAction
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.models import (
    AssistantResponseKind,
    RiskLevel,
    ToolArguments,
    ToolPreparation,
    ToolResult,
)
from desktop_assistant.process_control import (
    CloseRequestResult,
    ProcessControlError,
    WindowController,
    WindowInfo,
    WindowsAppProcessController,
    WindowsWindowController,
    _ProcessEntry,
    _WindowApi,
    _WindowEntry,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.windows import (
    FocusWindowTool,
    PreparedWindowFocus,
    WindowInfoTool,
    match_window_for_focus,
)

from conftest import (
    FakeLauncher,
    FakeMediaController,
    FakeProcessController,
    FakeProjectTaskRunner,
    FakeSystemStatusCollector,
    FakeVSCodeLauncher,
    FakeWindowController,
    make_registry,
)


class FakeProvider:
    def __init__(self, result: IntentResult | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    def resolve(self, request: str) -> IntentResult:
        self.calls.append(request)
        assert self.result is not None
        return self.result


def _build_test_assistant(
    window_controller: FakeWindowController,
    launcher: FakeLauncher | None = None,
    provider: FakeProvider | None = None,
    catalog: AppCatalog | None = None,
) -> Assistant:
    fake_launcher = launcher or FakeLauncher()
    app_catalog = catalog or AppCatalog()
    registry = make_registry(
        fake_launcher,
        window_controller=window_controller,
    )
    router = CommandRouter(registry, app_catalog)
    return Assistant(router, registry, provider)


# ---------------------------------------------------------------------------
# Unit tests for matching strategy
# ---------------------------------------------------------------------------


def test_match_window_exact_title() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=101, process_id=10, title="Document1 - Word", executable_name="WINWORD.EXE")
    w2 = WindowInfo(handle=102, process_id=20, title="Calculator", executable_name="CalculatorApp.exe")
    windows = (w1, w2)

    matched = match_window_for_focus("Calculator", windows, catalog)
    assert isinstance(matched, WindowInfo)
    assert matched.handle == 102
    assert matched.title == "Calculator"


def test_match_window_exact_title_case_insensitive() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=101, process_id=10, title="Settings", executable_name="SystemSettings.exe")
    windows = (w1,)

    matched = match_window_for_focus("  settings  ", windows, catalog)
    assert isinstance(matched, WindowInfo)
    assert matched.handle == 101


def test_match_window_duplicate_exact_title_arbitrary_executables_is_ambiguous() -> None:
    catalog = AppCatalog()
    # 1. Arbitrary executables not in AppCatalog
    w1 = WindowInfo(handle=101, process_id=10, title="Editor", executable_name="editor1.exe")
    w2 = WindowInfo(handle=102, process_id=20, title="Editor", executable_name="editor2.exe")
    windows = (w1, w2)

    matched = match_window_for_focus("Editor", windows, catalog)
    assert isinstance(matched, str)
    assert "Multiple windows match 'Editor'" in matched
    assert "'Editor' (editor1.exe)" in matched
    assert "'Editor' (editor2.exe)" in matched

    # 2. Query is AppCatalog alias ("Notepad") but executables are arbitrary / non-allowlisted
    w3 = WindowInfo(handle=103, process_id=30, title="Notepad", executable_name="a.exe")
    w4 = WindowInfo(handle=104, process_id=40, title="Notepad", executable_name="b.exe")
    windows2 = (w3, w4)

    matched2 = match_window_for_focus("Notepad", windows2, catalog)
    assert isinstance(matched2, str)
    assert "Multiple windows match 'Notepad'" in matched2
    assert "'Notepad' (a.exe)" in matched2
    assert "'Notepad' (b.exe)" in matched2


def test_match_window_duplicate_exact_title_trusted_app_catalog_resolves() -> None:
    catalog = AppCatalog()
    # Query is trusted AppCatalog alias ("Notepad"), one is trusted, one is imposter
    w1 = WindowInfo(handle=201, process_id=10, title="Notepad", executable_name="notepad.exe")
    w2 = WindowInfo(handle=202, process_id=20, title="Notepad", executable_name="imposter.exe")
    windows = (w1, w2)

    matched = match_window_for_focus("Notepad", windows, catalog)
    assert isinstance(matched, WindowInfo)
    assert matched.handle == 201
    assert matched.executable_name == "notepad.exe"

    # Multiple visible trusted matches must remain ambiguous.
    w3 = WindowInfo(handle=203, process_id=30, title="Notepad", executable_name="notepad.exe")
    w4 = WindowInfo(handle=204, process_id=40, title="Notepad", executable_name="notepad.exe")
    windows_multi = (w3, w4)

    matched_multi = match_window_for_focus("Notepad", windows_multi, catalog)
    assert isinstance(matched_multi, str)
    assert "Multiple windows" in matched_multi


def test_match_window_app_catalog_alias_single() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=101, process_id=10, title="Inbox - Mail", executable_name="HxMail.exe")
    w2 = WindowInfo(
        handle=102,
        process_id=20,
        title="main.py - AiProject - Visual Studio Code",
        executable_name="Code.exe",
    )
    windows = (w1, w2)

    # "VS Code" is an AppCatalog alias that maps to Code.exe
    matched = match_window_for_focus("VS Code", windows, catalog)
    assert isinstance(matched, WindowInfo)
    assert matched.handle == 102
    assert matched.executable_name == "Code.exe"


def test_match_window_app_catalog_alias_multiple_visible_windows_is_ambiguous() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=201, process_id=30, title="AiProject - Visual Studio Code", executable_name="Code.exe")
    w2 = WindowInfo(handle=202, process_id=30, title="OtherProject - Visual Studio Code", executable_name="Code.exe")
    windows = (w1, w2)

    # Window ordering must not conceal ambiguity.
    matched = match_window_for_focus("VS Code", windows, catalog)
    assert isinstance(matched, str)
    assert "Multiple windows" in matched


def test_match_window_title_substring_single() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=301, process_id=40, title="Bookish - Chapter 1", executable_name="bookish.exe")
    w2 = WindowInfo(handle=302, process_id=50, title="Calculator", executable_name="calc.exe")
    windows = (w1, w2)

    matched = match_window_for_focus("Bookish", windows, catalog)
    assert isinstance(matched, WindowInfo)
    assert matched.handle == 301


def test_match_window_title_substring_ambiguous_returns_candidates() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=301, process_id=40, title="Bookish - Chapter 1", executable_name="bookish.exe")
    w2 = WindowInfo(handle=302, process_id=40, title="Bookish - Chapter 2", executable_name="bookish.exe")
    windows = (w1, w2)

    matched = match_window_for_focus("Bookish", windows, catalog)
    assert isinstance(matched, str)
    assert "Multiple windows match 'Bookish'" in matched
    assert "'Bookish - Chapter 1'" in matched
    assert "'Bookish - Chapter 2'" in matched


def test_match_window_no_match() -> None:
    catalog = AppCatalog()
    w1 = WindowInfo(handle=101, process_id=10, title="Calculator", executable_name="calc.exe")
    windows = (w1,)

    matched = match_window_for_focus("Photoshop", windows, catalog)
    assert isinstance(matched, str)
    assert "No matching window found" in matched


def test_match_window_empty_query_and_empty_windows() -> None:
    catalog = AppCatalog()
    assert isinstance(match_window_for_focus("", (), catalog), str)
    assert isinstance(match_window_for_focus("Chrome", (), catalog), str)


# ---------------------------------------------------------------------------
# Tool unit tests: window_info
# ---------------------------------------------------------------------------


def test_window_info_list_visible_windows() -> None:
    w1 = WindowInfo(handle=101, process_id=10, title="VS Code", executable_name="Code.exe")
    w2 = WindowInfo(handle=102, process_id=20, title="Spotify Free", executable_name="Spotify.exe")
    controller = FakeWindowController(windows=(w1, w2))
    tool = WindowInfoTool(controller)

    prep = tool.prepare(ToolArguments((("action", "list"),)))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert "Visible windows:" in result.message
    assert "VS Code — Code.exe" in result.message
    assert "Spotify Free — Spotify.exe" in result.message
    # No handle or PID in output or details
    assert "101" not in result.message
    assert "10" not in result.message
    assert result.details["windows"] == [
        {"title": "VS Code", "executable": "Code.exe"},
        {"title": "Spotify Free", "executable": "Spotify.exe"},
    ]


def test_window_info_list_empty() -> None:
    controller = FakeWindowController(windows=())
    tool = WindowInfoTool(controller)

    prep = tool.prepare(ToolArguments((("action", "list"),)))
    result = tool.execute(prep.execution_value)
    assert result.success
    assert result.message == "No visible windows found."
    assert result.details["windows"] == []


def test_window_info_active_window_present() -> None:
    active = WindowInfo(handle=501, process_id=60, title="Google Chrome", executable_name="chrome.exe")
    controller = FakeWindowController(active_window=active)
    tool = WindowInfoTool(controller)

    prep = tool.prepare(ToolArguments((("action", "active"),)))
    result = tool.execute(prep.execution_value)
    assert result.success
    assert result.message == "Active window: Google Chrome — chrome.exe"
    assert "501" not in result.message
    assert result.details == {"title": "Google Chrome", "executable": "chrome.exe"}


def test_window_info_active_window_absent() -> None:
    controller = FakeWindowController(active_window=None)
    tool = WindowInfoTool(controller)

    prep = tool.prepare(ToolArguments((("action", "active"),)))
    result = tool.execute(prep.execution_value)
    assert result.success
    assert result.message == "No active window detected."
    assert result.details == {}


def test_window_info_rejects_invalid_action() -> None:
    tool = WindowInfoTool(FakeWindowController())
    prep = tool.prepare(ToolArguments((("action", "invalid_action"),)))
    assert isinstance(prep, ToolResult)
    assert not prep.success
    assert "Unsupported window_info action" in prep.message


# ---------------------------------------------------------------------------
# Tool unit tests: focus_window
# ---------------------------------------------------------------------------


def test_focus_window_success() -> None:
    w1 = WindowInfo(handle=101, process_id=10, title="Document - Notepad", executable_name="notepad.exe")
    controller = FakeWindowController(windows=(w1,))
    tool = FocusWindowTool(controller, AppCatalog())

    prep = tool.prepare(ToolArguments((("query", "Notepad"),)))
    assert isinstance(prep, ToolPreparation)
    assert isinstance(prep.execution_value, PreparedWindowFocus)
    assert prep.execution_value.handle == 101
    assert prep.execution_value.title == "Document - Notepad"

    result = tool.execute(prep.execution_value)
    assert result.success
    assert result.risk_level == RiskLevel.SAFE
    assert result.message == "Switched to Document - Notepad."
    assert controller.focused_handles == [101]
    assert result.details == {"title": "Document - Notepad", "executable": "notepad.exe"}


def test_focus_window_restores_minimized_window_before_focus() -> None:
    w1 = WindowInfo(
        handle=202,
        process_id=20,
        title="Spotify",
        executable_name="Spotify.exe",
        minimized=True,
    )
    controller = FakeWindowController(windows=(w1,))
    tool = FocusWindowTool(controller, AppCatalog())

    prep = tool.prepare(ToolArguments((("query", "Spotify"),)))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert result.success
    assert controller.restored_handles == [202]
    assert controller.focused_handles == [202]


def test_focus_window_fails_when_window_disappears() -> None:
    w1 = WindowInfo(handle=303, process_id=30, title="Temporary Window", executable_name="temp.exe")
    controller = FakeWindowController(windows=(w1,))
    tool = FocusWindowTool(controller, AppCatalog())

    prep = tool.prepare(ToolArguments((("query", "Temporary Window"),)))
    assert isinstance(prep, ToolPreparation)

    # Simulate window disappearing before execution
    controller.valid_handles.remove(303)

    result = tool.execute(prep.execution_value)
    assert not result.success
    assert "is no longer available" in result.message
    assert controller.focused_handles == []


def test_focus_window_fails_safely_when_windows_refuses_focus() -> None:
    w1 = WindowInfo(handle=404, process_id=40, title="Protected Window", executable_name="prot.exe")
    controller = FakeWindowController(windows=(w1,), focus_succeeds=False)
    tool = FocusWindowTool(controller, AppCatalog())

    prep = tool.prepare(ToolArguments((("query", "Protected Window"),)))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert not result.success
    assert "Windows could not bring 'Protected Window' to the foreground" in result.message


def test_prepared_window_focus_is_immutable() -> None:
    prepared = PreparedWindowFocus(handle=1, process_id=2, title="T", executable_name="E")
    with pytest.raises((AttributeError, TypeError)):
        prepared.handle = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Router & Plan Integration
# ---------------------------------------------------------------------------


def test_router_deterministic_window_commands() -> None:
    router = CommandRouter()

    d1 = router.route_detailed("windows")
    assert d1.recognized
    assert d1.action is not None
    assert d1.action.tool_name == "window_info"
    assert d1.action.arguments == {"action": "list"}

    d2 = router.route_detailed("list windows")
    assert d2.recognized
    assert d2.action is not None
    assert d2.action.tool_name == "window_info"
    assert d2.action.arguments == {"action": "list"}

    d3 = router.route_detailed("active window")
    assert d3.recognized
    assert d3.action is not None
    assert d3.action.tool_name == "window_info"
    assert d3.action.arguments == {"action": "active"}

    d4 = router.route_detailed("focus VS Code")
    assert d4.recognized
    assert d4.action is not None
    assert d4.action.tool_name == "focus_window"
    assert d4.action.arguments == {"query": "VS Code"}

    d5 = router.route_detailed("switch to Chrome")
    assert d5.recognized
    assert d5.action is not None
    assert d5.action.tool_name == "focus_window"
    assert d5.action.arguments == {"query": "Chrome"}


def test_multi_step_plan_with_open_app_and_focus_window() -> None:
    launcher = FakeLauncher()
    w1 = WindowInfo(handle=501, process_id=50, title="Visual Studio Code", executable_name="Code.exe")
    controller = FakeWindowController(windows=(w1,))

    plan = ActionPlan((
        ToolAction("open_app", {"app_name": "Spotify"}),
        ToolAction("focus_window", {"query": "VS Code"}),
    ))
    provider = FakeProvider(IntentResult.action_plan(plan.actions))

    assistant = _build_test_assistant(controller, launcher=launcher, provider=provider)
    response = assistant.handle("Άνοιξε το Spotify και μετά γύρνα στο VS Code.")

    assert response.kind is AssistantResponseKind.COMPLETED
    assert response.success
    assert [app.display_name for app in launcher.apps] == ["Spotify"]
    assert controller.focused_handles == [501]
    assert "Completed 2 actions:" in response.result.message
    assert "Opening Spotify." in response.result.message
    assert "Switched to Visual Studio Code." in response.result.message


def test_existing_close_app_behavior_is_unchanged() -> None:
    class FakeProcApi:
        def processes(self) -> tuple[_ProcessEntry, ...]:
            return (_ProcessEntry(process_id=123, executable_name="Spotify.exe"),)

        def top_level_windows(self) -> tuple[_WindowEntry, ...]:
            return (_WindowEntry(handle=456, process_id=123),)

        def post_close(self, window_handle: int) -> bool:
            return window_handle == 456

    controller = WindowsAppProcessController(FakeProcApi())  # type: ignore[arg-type]
    catalog = AppCatalog()
    app = catalog.resolve("Spotify")
    assert app is not None

    assert controller.is_running(app)
    res = controller.request_close(app)
    assert res.was_running
    assert res.requested_window_count == 1


# ---------------------------------------------------------------------------
# Windows mock integration test for WindowsWindowController
# ---------------------------------------------------------------------------


class MockWindowApi:
    def __init__(self) -> None:
        self.windows = (
            WindowInfo(handle=10, process_id=100, title="Notepad", executable_name="notepad.exe"),
            WindowInfo(handle=20, process_id=200, title="Calculator", executable_name="calc.exe", minimized=True),
        )
        self.foreground = self.windows[0]
        self.restored: list[int] = []
        self.focused: list[int] = []

    def visible_windows(self, max_count: int = 30) -> tuple[WindowInfo, ...]:
        return self.windows[:max_count]

    def get_foreground_window(self) -> WindowInfo | None:
        return self.foreground

    def is_window(self, handle: int) -> bool:
        return handle in {10, 20}

    def get_window_thread_process_id(self, handle: int) -> int:
        for w in self.windows:
            if w.handle == handle:
                return w.process_id
        return 0

    def is_iconic(self, handle: int) -> bool:
        for w in self.windows:
            if w.handle == handle:
                return w.minimized
        return False

    def show_window(self, handle: int, cmd: int) -> bool:
        self.restored.append(handle)
        return True

    def set_foreground_window(self, handle: int) -> bool:
        self.focused.append(handle)
        return True


def test_windows_window_controller_with_mock_api() -> None:
    api = MockWindowApi()
    controller = WindowsWindowController(api)

    visible = controller.visible_windows()
    assert len(visible) == 2
    assert visible[0].title == "Notepad"

    fg = controller.get_foreground_window()
    assert fg is not None
    assert fg.title == "Notepad"

    assert controller.is_window_valid(10, 100)
    assert not controller.is_window_valid(10, 999)
    assert not controller.is_window_valid(999, 100)

    assert controller.is_minimized(20)
    assert not controller.is_minimized(10)

    assert controller.restore_window(20)
    assert api.restored == [20]

    assert controller.set_foreground_window(10)
    assert api.focused == [10]


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_windows_real_window_controller_read_only_smoke() -> None:
    controller = WindowsWindowController()
    # Read-only inspection smoke test - must never crash or change focus
    windows = controller.visible_windows()
    assert isinstance(windows, tuple)
    fg = controller.get_foreground_window()
    assert fg is None or isinstance(fg, WindowInfo)
