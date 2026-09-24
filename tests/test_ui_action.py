from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from conftest import FakeLauncher, FakeWindowController, make_registry
from desktop_assistant.assistant import Assistant
from desktop_assistant.bootstrap import build_assistant
from desktop_assistant.config import AppCatalog, Settings
from desktop_assistant.intent.models import IntentKind, IntentResult, ToolAction
from desktop_assistant.models import RiskLevel, ToolPreparation, ToolResult
from desktop_assistant.process_control import WindowInfo
from desktop_assistant.router import CommandRouter
from desktop_assistant.ui_action import (
    PreparedControlIdentity,
    PreparedUIAction,
    UIAction,
    UIActionTool,
    WindowsUIActionController,
)
from desktop_assistant.ui_perception import (
    MAX_SCANNED_ELEMENTS,
    _CONTROL_TYPES,
    _PATTERNS,
)


TARGET = WindowInfo(71, 17, "Untitled - Notepad", "notepad.exe", False)


def types():
    values = {
        "UIA_ControlTypePropertyId": 30003,
        "UIA_WindowControlTypeId": 50032,
        "UIA_TextControlTypeId": 50020,
        "UIA_PaneControlTypeId": 50033,
        "UIA_RuntimeIdPropertyId": 30000,
        "TreeScope_Descendants": 4,
        "UIA_InvokePatternId": 10000,
        "UIA_SelectionItemPatternId": 10010,
        "UIA_ExpandCollapsePatternId": 10005,
    }
    for index, (raw, _) in enumerate(_CONTROL_TYPES):
        values[f"UIA_{raw}ControlTypeId"] = 51000 + index
    for index, (raw, _) in enumerate((*_PATTERNS, ("Value", ""), ("Text", ""), ("TextEdit", ""))):
        values[f"UIA_Is{raw}PatternAvailablePropertyId"] = 31000 + index
    return SimpleNamespace(**values)


T = types()


class MockInvokePattern:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def Invoke(self):
        if self.fail:
            raise OSError("COM Invoke failed")
        self.calls += 1


class MockSelectionItemPattern:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def Select(self):
        if self.fail:
            raise OSError("COM Select failed")
        self.calls += 1


class MockExpandCollapsePattern:
    def __init__(self, fail: bool = False):
        self.expand_calls = 0
        self.collapse_calls = 0
        self.fail = fail

    def Expand(self):
        if self.fail:
            raise OSError("COM Expand failed")
        self.expand_calls += 1

    def Collapse(self):
        if self.fail:
            raise OSError("COM Collapse failed")
        self.collapse_calls += 1


class Element:
    def __init__(
        self,
        kind,
        parent=None,
        pid=17,
        *,
        handle=0,
        name="Name",
        automation_id="id",
        password=False,
        patterns=(),
        runtime_id=(42, 1),
        enabled=True,
    ):
        self.parent = parent
        self.CurrentControlType = kind
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = handle
        self._name = name
        self._automation_id = automation_id
        self.CurrentIsPassword = password
        self.CurrentIsEnabled = enabled
        self.CurrentIsKeyboardFocusable = True
        self.CurrentHasKeyboardFocus = False
        self.CurrentIsOffscreen = False
        self.patterns = set(patterns)
        self.runtime_id = runtime_id
        self.children = []
        self.pattern_objects = {}

    @property
    def CurrentName(self):
        if self.CurrentIsPassword:
            raise AssertionError("Password name was read")
        return self._name

    @property
    def CurrentAutomationId(self):
        if self.CurrentIsPassword:
            raise AssertionError("Password automation ID was read")
        return self._automation_id

    def GetRuntimeId(self):
        return self.runtime_id

    def GetCurrentPattern(self, pattern_id):
        return self.pattern_objects.get(pattern_id)

    def GetCurrentPropertyValue(self, prop):
        if prop == T.UIA_RuntimeIdPropertyId:
            return self.runtime_id
        return prop in self.patterns

    def FindAll(self, scope, condition):
        return SimpleNamespace(
            Length=len(self.children),
            GetElement=lambda index: self.children[index],
        )


class Automation:
    def __init__(self, root):
        self.root = root
        self.handles = []
        self.RawViewWalker = SimpleNamespace(GetParentElement=lambda element: element.parent)

    def ElementFromHandle(self, handle):
        self.handles.append(handle)
        return self.root

    def CreateTrueCondition(self):
        return "true-condition"

    def CompareElements(self, left, right):
        return left is right


class Backend:
    def __init__(self, automation):
        self.automation = automation

    @contextmanager
    def open(self):
        yield self.automation, T


def make_controller(*children):
    root = Element(T.UIA_WindowControlTypeId, handle=TARGET.handle, pid=TARGET.process_id)
    for child in children:
        if child.parent is None:
            child.parent = root
    root.children = list(children)
    automation = Automation(root)
    return root, WindowsUIActionController(Backend(automation))


def make_harness(windows=(TARGET,), *children):
    root, controller = make_controller(*children)
    win_ctrl = FakeWindowController(tuple(windows))
    catalog = AppCatalog()
    tool = UIActionTool(win_ctrl, catalog, controller)
    registry = make_registry(FakeLauncher(), window_controller=win_ctrl, ui_action_controller=controller)
    return win_ctrl, controller, tool, registry, root


def test_exact_name_match():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(1, 1))
    b2 = Element(T.UIA_ButtonControlTypeId, name="Settings and More", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(1, 2))
    _, _, tool, _, _ = make_harness((TARGET,), b1, b2)

    prepared = tool.prepare({"query": "Notepad", "control": "Settings", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)
    assert prepared.execution_value.name == "Settings"
    assert prepared.execution_value.runtime_id == (1, 1)
    assert prepared.execution_value.control_type == "button"


def test_unique_substring_match():
    tab1 = Element(T.UIA_TabItemControlTypeId, name="Untitled. Unmodified.", patterns=(T.UIA_IsSelectionItemPatternAvailablePropertyId,), runtime_id=(2, 1))
    tab2 = Element(T.UIA_TabItemControlTypeId, name="Other Document", patterns=(T.UIA_IsSelectionItemPatternAvailablePropertyId,), runtime_id=(2, 2))
    _, _, tool, _, _ = make_harness((TARGET,), tab1, tab2)

    prepared = tool.prepare({"query": "Notepad", "control": "Untitled", "action": "select"})
    assert isinstance(prepared, ToolPreparation)
    assert prepared.execution_value.name == "Untitled. Unmodified."
    assert prepared.execution_value.runtime_id == (2, 1)


def test_ambiguous_name_rejected():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Close", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(3, 1))
    b2 = Element(T.UIA_ButtonControlTypeId, name="Close", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(3, 2))
    _, _, tool, _, _ = make_harness((TARGET,), b1, b2)

    result = tool.prepare({"query": "Notepad", "control": "Close", "action": "invoke"})
    assert isinstance(result, ToolResult)
    assert not result.success
    assert "Multiple matching controls found for 'Close'" in result.message
    assert "button — 'Close'" in result.message

    # Substring ambiguity
    t1 = Element(T.UIA_TabItemControlTypeId, name="Doc 1", patterns=(T.UIA_IsSelectionItemPatternAvailablePropertyId,), runtime_id=(4, 1))
    t2 = Element(T.UIA_TabItemControlTypeId, name="Doc 2", patterns=(T.UIA_IsSelectionItemPatternAvailablePropertyId,), runtime_id=(4, 2))
    _, _, tool2, _, _ = make_harness((TARGET,), t1, t2)

    result2 = tool2.prepare({"query": "Notepad", "control": "Doc", "action": "select"})
    assert isinstance(result2, ToolResult)
    assert not result2.success
    assert "Multiple matching controls found for 'Doc'" in result2.message


def test_no_match():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Save", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(5, 1))
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    result = tool.prepare({"query": "Notepad", "control": "Open", "action": "invoke"})
    assert isinstance(result, ToolResult)
    assert not result.success
    assert result.message == "No matching actionable control found."


def test_action_capability_filters_candidates():
    m1 = Element(T.UIA_MenuItemControlTypeId, name="File", patterns=(T.UIA_IsExpandCollapsePatternAvailablePropertyId,), runtime_id=(6, 1))
    _, _, tool, _, _ = make_harness((TARGET,), m1)

    # File does not have InvokePattern
    res_inv = tool.prepare({"query": "Notepad", "control": "File", "action": "invoke"})
    assert isinstance(res_inv, ToolResult)
    assert res_inv.message == "No matching actionable control found."

    # File has ExpandCollapsePattern
    res_exp = tool.prepare({"query": "Notepad", "control": "File", "action": "expand"})
    assert isinstance(res_exp, ToolPreparation)
    assert res_exp.execution_value.name == "File"


def test_disabled_control_rejected():
    b1 = Element(T.UIA_ButtonControlTypeId, name="DisabledBtn", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(7, 1), enabled=False)
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    result = tool.prepare({"query": "Notepad", "control": "DisabledBtn", "action": "invoke"})
    assert isinstance(result, ToolResult)
    assert result.message == "No matching actionable control found."


def test_wrong_pid_rejected():
    b1 = Element(T.UIA_ButtonControlTypeId, name="OtherPid", pid=999, patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(8, 1))
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    result = tool.prepare({"query": "Notepad", "control": "OtherPid", "action": "invoke"})
    assert isinstance(result, ToolResult)
    assert result.message == "No matching actionable control found."


def test_outside_root_control_rejected():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Outside", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(9, 1))
    b1.parent = Element(T.UIA_WindowControlTypeId, name="Foreign Window")  # parent is foreign window, not root
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    result = tool.prepare({"query": "Notepad", "control": "Outside", "action": "invoke"})
    assert isinstance(result, ToolResult)
    assert result.message == "No matching actionable control found."


def test_stale_runtime_id_at_confirm_zero_action():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Button", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(10, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, root = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Button", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)

    # Element disappears from root
    root.children = []

    res = tool.execute(prepared.execution_value)
    assert not res.success
    assert "could not be located uniquely" in res.message
    assert pat.calls == 0


def test_changed_name_zero_action():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Button", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(11, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Button", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)

    # Change name
    b1._name = "Renamed Button"

    res = tool.execute(prepared.execution_value)
    assert not res.success
    assert "has changed or is no longer available" in res.message
    assert pat.calls == 0


def test_changed_automation_id_zero_action():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Button", automation_id="btn_1", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(12, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Button", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)

    b1._automation_id = "btn_modified"

    res = tool.execute(prepared.execution_value)
    assert not res.success
    assert "has changed or is no longer available" in res.message
    assert pat.calls == 0


def test_changed_type_zero_action():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Button", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(13, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Button", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)

    b1.CurrentControlType = T.UIA_EditControlTypeId

    res = tool.execute(prepared.execution_value)
    assert not res.success
    assert "has changed or is no longer available" in res.message
    assert pat.calls == 0


@pytest.mark.parametrize("mutator", [
    lambda w: replace(w, title="Renamed - Notepad"),
    lambda w: replace(w, process_id=999),
    lambda w: replace(w, executable_name="other.exe"),
])
def test_changed_top_level_target_zero_action(mutator):
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Button", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(14, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    win_ctrl, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Button", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)

    win_ctrl.windows = [mutator(TARGET)]

    res = tool.execute(prepared.execution_value)
    assert not res.success
    assert "changed or is no longer available" in res.message
    assert pat.calls == 0


def test_duplicate_runtime_id_match_zero_action():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Button", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(15, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, root = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Button", "action": "invoke"})
    assert isinstance(prepared, ToolPreparation)

    # Another element sharing the same RuntimeId appears
    b2 = Element(T.UIA_ButtonControlTypeId, name="DupButton", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(15, 1))
    b2.parent = root
    root.children.append(b2)

    res = tool.execute(prepared.execution_value)
    assert not res.success
    assert "could not be located uniquely" in res.message
    assert pat.calls == 0


def test_invoke_calls_only_invoke():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(16, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Settings", "action": "invoke"})
    res = tool.execute(prepared.execution_value)

    assert res.success
    assert pat.calls == 1


def test_select_calls_only_select():
    pat = MockSelectionItemPattern()
    tab = Element(T.UIA_TabItemControlTypeId, name="Untitled", patterns=(T.UIA_IsSelectionItemPatternAvailablePropertyId,), runtime_id=(17, 1))
    tab.pattern_objects[T.UIA_SelectionItemPatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), tab)

    prepared = tool.prepare({"query": "Notepad", "control": "Untitled", "action": "select"})
    res = tool.execute(prepared.execution_value)

    assert res.success
    assert pat.calls == 1


def test_expand_calls_only_expand():
    pat = MockExpandCollapsePattern()
    menu = Element(T.UIA_MenuItemControlTypeId, name="File", patterns=(T.UIA_IsExpandCollapsePatternAvailablePropertyId,), runtime_id=(18, 1))
    menu.pattern_objects[T.UIA_ExpandCollapsePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), menu)

    prepared = tool.prepare({"query": "Notepad", "control": "File", "action": "expand"})
    res = tool.execute(prepared.execution_value)

    assert res.success
    assert pat.expand_calls == 1
    assert pat.collapse_calls == 0


def test_collapse_calls_only_collapse():
    pat = MockExpandCollapsePattern()
    menu = Element(T.UIA_MenuItemControlTypeId, name="File", patterns=(T.UIA_IsExpandCollapsePatternAvailablePropertyId,), runtime_id=(19, 1))
    menu.pattern_objects[T.UIA_ExpandCollapsePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), menu)

    prepared = tool.prepare({"query": "Notepad", "control": "File", "action": "collapse"})
    res = tool.execute(prepared.execution_value)

    assert res.success
    assert pat.collapse_calls == 1
    assert pat.expand_calls == 0


def test_com_action_failure_reported_safely():
    pat = MockInvokePattern(fail=True)
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(20, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Settings", "action": "invoke"})
    res = tool.execute(prepared.execution_value)

    assert not res.success
    assert res.message == "Failed to perform invoke on 'Settings'."
    # No COM exception content or traceback in message
    assert "OSError" not in res.message
    assert "COM Invoke failed" not in res.message


def test_prepared_payload_frozen():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(21, 1))
    _, _, tool, _, _ = make_harness((TARGET,), b1)

    prepared = tool.prepare({"query": "Notepad", "control": "Settings", "action": "invoke"})
    val = prepared.execution_value
    assert isinstance(val, PreparedUIAction)
    with pytest.raises(FrozenInstanceError):
        val.name = "Modified"


def test_cancel_zero_mutation():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(22, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    win_ctrl, controller, _, _, _ = make_harness((TARGET,), b1)

    provider = SimpleNamespace(
        resolve=lambda req: IntentResult.tool_action(
            "ui_action", {"query": "Notepad", "control": "Settings", "action": "invoke"}
        )
    )

    assistant = build_assistant(
        settings=Settings(openai_api_key="test"),
        window_controller=win_ctrl,
        ui_action_controller=controller,
        intent_provider=provider,
    )

    out = assistant.handle("Πάτα Settings στο Notepad.")
    assert out.confirmation is not None
    assert "Target window" in out.confirmation.summary
    assert assistant.has_pending_confirmation()
    assert pat.calls == 0

    assistant.cancel(out.confirmation.confirmation_id)
    assert not assistant.has_pending_confirmation()
    assert pat.calls == 0


def test_confirm_no_second_provider_call():
    pat = MockInvokePattern()
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(23, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = pat
    win_ctrl, controller, _, _, _ = make_harness((TARGET,), b1)

    provider_calls = 0

    def fake_resolve(req):
        nonlocal provider_calls
        provider_calls += 1
        return IntentResult.tool_action(
            "ui_action", {"query": "Notepad", "control": "Settings", "action": "invoke"}
        )

    provider = SimpleNamespace(resolve=fake_resolve)

    assistant = build_assistant(
        settings=Settings(openai_api_key="test"),
        window_controller=win_ctrl,
        ui_action_controller=controller,
        intent_provider=provider,
    )

    out = assistant.handle("Πάτα Settings στο Notepad.")
    assert provider_calls == 1
    assert out.confirmation is not None

    confirm_res = assistant.confirm(out.confirmation.confirmation_id)
    assert provider_calls == 1  # No second provider call!
    assert pat.calls == 1
    assert "Performed invoke on 'Settings'" in confirm_res.message


def test_runtime_id_hwnd_pid_absent_from_schema_results_confirmation():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(24, 1))
    b1.pattern_objects[T.UIA_InvokePatternId] = MockInvokePattern()
    _, _, tool, registry, _ = make_harness((TARGET,), b1)

    # 1. Schema
    schemas = registry.schemas()
    ui_act = next(s for s in schemas if s["name"] == "ui_action")
    props = ui_act["parameters"]["properties"]
    assert "runtime_id" not in props
    assert "handle" not in props
    assert "hwnd" not in props
    assert "pid" not in props

    # 2. Confirmation summary
    prepared = registry.prepare("ui_action", {"query": "Notepad", "control": "Settings", "action": "invoke"})
    assert isinstance(prepared.summary, str)
    assert "Target window:\nUntitled - Notepad — notepad.exe" in prepared.summary
    assert "Control:\nbutton — Settings" in prepared.summary
    assert "Action:\ninvoke" in prepared.summary
    assert "runtime_id" not in prepared.summary
    assert "71" not in prepared.summary
    assert "17" not in prepared.summary

    # 3. ToolResult
    result = tool.execute(prepared.execution_value)
    assert result.success
    assert "71" not in result.message
    assert "17" not in result.message
    assert "runtime_id" not in result.details
    assert "handle" not in result.details
    assert "process_id" not in result.details


def test_ui_action_sensitive():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(25, 1))
    _, _, _, registry, _ = make_harness((TARGET,), b1)

    prep = registry.prepare("ui_action", {"query": "Notepad", "control": "Settings", "action": "invoke"})
    assert prep.risk_level == RiskLevel.SENSITIVE


def test_existing_ui_inspect_remains_safe():
    b1 = Element(T.UIA_ButtonControlTypeId, name="Settings", patterns=(T.UIA_IsInvokePatternAvailablePropertyId,), runtime_id=(26, 1))
    _, _, _, registry, _ = make_harness((TARGET,), b1)

    prep = registry.prepare("ui_inspect", {"query": "Notepad"})
    assert prep.risk_level == RiskLevel.SAFE
    res = registry.dispatch_prepared(prep)
    assert res.success  # Dispatches without confirmation
