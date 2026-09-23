from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from conftest import FakeLauncher, FakeUIInspector, FakeWindowController, make_registry
from desktop_assistant.bootstrap import build_assistant
from desktop_assistant.config import Settings
from desktop_assistant.intent.models import IntentResult
from desktop_assistant.intent.openai_provider import _INSTRUCTIONS
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.process_control import WindowInfo
from desktop_assistant.ui_perception import (
    MAX_CONTROLS, MAX_SCANNED_ELEMENTS, InspectionStage,
    UIElementInfo, UIInspection, UIInspectionError, WindowsUIInspector,
    _CONTROL_TYPES, _PATTERNS,
)


TARGET = WindowInfo(71, 17, "Untitled - Notepad", "notepad.exe", False)
OTHER = WindowInfo(72, 18, "Other - Notepad", "notepad.exe", False)


def types():
    values = {
        "UIA_ControlTypePropertyId": 30003,
        "UIA_WindowControlTypeId": 50032,
        "UIA_TextControlTypeId": 50020,
        "UIA_PaneControlTypeId": 50033,
        "UIA_ValueIsReadOnlyPropertyId": 30046,
        "TreeScope_Descendants": 4,
    }
    for index, (raw, _) in enumerate(_CONTROL_TYPES):
        values[f"UIA_{raw}ControlTypeId"] = 51000 + index
    for index, (raw, _) in enumerate((*_PATTERNS, ("Value", ""), ("Text", ""), ("TextEdit", ""))):
        values[f"UIA_Is{raw}PatternAvailablePropertyId"] = 31000 + index
    return SimpleNamespace(**values)


T = types()


class Element:
    def __init__(self, kind, parent=None, pid=17, *, handle=0, name="Name", automation_id="id",
                 password=False, patterns=(), readonly=False):
        self.parent = parent
        self.CurrentControlType = kind
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = handle
        self._name = name
        self._automation_id = automation_id
        self.CurrentIsPassword = password
        self.CurrentIsEnabled = True
        self.CurrentIsKeyboardFocusable = True
        self.CurrentHasKeyboardFocus = False
        self.CurrentIsOffscreen = False
        self.patterns = set(patterns)
        self.readonly = readonly
        self.children = []
        self.searches = []
        self.reads = []

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

    def GetCurrentPropertyValue(self, prop):
        self.reads.append(prop)
        if prop == T.UIA_ValueIsReadOnlyPropertyId:
            return self.readonly
        return prop in self.patterns

    def FindAll(self, scope, condition):
        self.searches.append((scope, condition))
        return SimpleNamespace(Length=len(self.children), GetElement=lambda index: self.children[index])

    def __getattr__(self, name):
        if name in {"CurrentValue", "DocumentRange", "GetCurrentPattern", "SetFocus"}:
            raise AssertionError(f"Mutation/content API accessed: {name}")
        raise AttributeError(name)


class FaultyElement(Element):
    def __init__(self, *args, fail_on=None, fail_property=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_on = fail_on
        self.fail_property = fail_property

    def __getattribute__(self, name):
        if name == object.__getattribute__(self, "fail_on"):
            raise OSError("private provider error")
        return super().__getattribute__(name)

    def GetCurrentPropertyValue(self, prop):
        if prop == self.fail_property:
            raise OSError("private provider error")
        return super().GetCurrentPropertyValue(prop)


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

    def CreateOrCondition(self, left, right):
        raise AssertionError("Composite conditions are forbidden")

    def CompareElements(self, left, right):
        return left is right

    def __getattr__(self, name):
        if name in {"GetRootElement", "GetFocusedElement", "SetFocus", "SetForegroundWindow"}:
            raise AssertionError(f"Forbidden UIA API: {name}")
        raise AttributeError(name)


class Backend:
    def __init__(self, automation):
        self.automation = automation

    @contextmanager
    def open(self):
        yield self.automation, T


def backend(*children):
    root = Element(T.UIA_WindowControlTypeId, handle=TARGET.handle)
    for child in children:
        if child.parent is None:
            child.parent = root
    root.children = list(children)
    automation = Automation(root)
    return root, automation, WindowsUIInspector(Backend(automation))


def tool_harness(windows=(TARGET,), inspection=None):
    controller = FakeWindowController(tuple(windows))
    inspector = FakeUIInspector(inspection)
    registry = make_registry(FakeLauncher(), window_controller=controller, ui_inspector=inspector)
    return controller, inspector, registry


def test_exact_prepared_target_is_immutable_and_no_focus_change():
    controller, inspector, registry = tool_harness()
    prepared = registry.prepare("ui_inspect", {"query": "Notepad"})
    assert prepared.risk_level is RiskLevel.SAFE
    target = prepared.execution_value
    assert (target.handle, target.process_id, target.title, target.executable_name) == (
        71, 17, "Untitled - Notepad", "notepad.exe")
    with pytest.raises(FrozenInstanceError):
        target.title = "changed"
    result = registry.dispatch_prepared(prepared)
    assert result.success and inspector.calls == [(71, 17)]
    assert controller.focused_handles == controller.restored_handles == []
    assert "No supported interactive controls" in result.message


def test_ambiguous_top_level_target_is_rejected():
    windows = (WindowInfo(71, 17, "Bookish - A", "bookish.exe", False),
               WindowInfo(72, 18, "Bookish - B", "bookish.exe", False))
    _, inspector, registry = tool_harness(windows)
    result = registry.execute("ui_inspect", {"query": "Bookish"})
    assert not result.success and "Multiple windows" in result.message
    assert not inspector.calls


@pytest.mark.parametrize("current", [
    None,
    replace(TARGET, process_id=999),
    replace(TARGET, title="Renamed - Notepad"),
    replace(TARGET, executable_name="other.exe"),
])
def test_stale_or_changed_target_rejected_before_inspection(current):
    controller, inspector, registry = tool_harness()
    prepared = registry.prepare("ui_inspect", {"query": "Notepad"})
    controller.windows = [] if current is None else [current]
    result = registry.dispatch_prepared(prepared)
    assert not result.success and inspector.calls == []


@pytest.mark.parametrize("field,value", [("CurrentNativeWindowHandle", 999), ("CurrentProcessId", 999)])
def test_uia_root_identity_mismatch_rejected(field, value):
    root, automation, inspector = backend(Element(T.UIA_ButtonControlTypeId))
    setattr(root, field, value)
    with pytest.raises(UIInspectionError):
        inspector.inspect(71, 17)
    assert automation.handles == [71] and root.searches == []


def test_root_element_from_handle_error_is_hard_failure():
    _, automation, inspector = backend(Element(T.UIA_ButtonControlTypeId))
    def fail_root(_handle):
        raise OSError("private provider error")
    automation.ElementFromHandle = fail_root
    with pytest.raises(UIInspectionError, match="could not be inspected safely") as error:
        inspector.inspect(71, 17)
    assert "private provider error" not in str(error.value)


@pytest.mark.parametrize("property_name", [
    "CurrentProcessId", "CurrentControlType", "CurrentIsPassword", "CurrentName",
    "CurrentAutomationId", "CurrentIsEnabled", "CurrentIsKeyboardFocusable",
    "CurrentHasKeyboardFocus", "CurrentIsOffscreen",
])
def test_one_bad_child_property_is_skipped_but_valid_button_is_returned(property_name):
    bad = FaultyElement(T.UIA_ButtonControlTypeId, name="unsafe", fail_on=property_name)
    good = Element(T.UIA_ButtonControlTypeId, name="Save")
    _, _, inspector = backend(bad, good)
    inspection = inspector.inspect(71, 17)
    assert [(control.control_type, control.name) for control in inspection.controls] == [
        ("button", "Save")]
    assert not inspection.truncated


def test_stale_child_during_ancestry_is_skipped():
    stale = FaultyElement(T.UIA_ButtonControlTypeId, name="stale")
    good = Element(T.UIA_ButtonControlTypeId, name="Settings")
    _, _, inspector = backend(stale, good)
    stale.fail_on = "parent"
    assert [control.name for control in inspector.inspect(71, 17).controls] == ["Settings"]


def test_capability_property_error_omits_only_that_capability():
    button = FaultyElement(
        T.UIA_ButtonControlTypeId, name="Save",
        patterns=(T.UIA_IsInvokePatternAvailablePropertyId,
                  T.UIA_IsTogglePatternAvailablePropertyId),
        fail_property=T.UIA_IsInvokePatternAvailablePropertyId,
    )
    _, _, inspector = backend(button)
    inspection = inspector.inspect(71, 17)
    assert len(inspection.controls) == 1
    assert inspection.controls[0].capabilities == ("toggle",)


def test_edit_readonly_property_error_omits_edit_but_keeps_control():
    edit = FaultyElement(
        T.UIA_EditControlTypeId, name="Editor",
        patterns=(T.UIA_IsValuePatternAvailablePropertyId,),
        fail_property=T.UIA_ValueIsReadOnlyPropertyId,
    )
    _, _, inspector = backend(edit)
    inspection = inspector.inspect(71, 17)
    assert len(inspection.controls) == 1
    assert inspection.controls[0].name == "Editor"
    assert "edit" not in inspection.controls[0].capabilities


def test_multiple_bad_children_do_not_abort_or_expose_partial_metadata():
    bad_name = FaultyElement(T.UIA_ButtonControlTypeId, name="private name", fail_on="CurrentName")
    bad_id = FaultyElement(T.UIA_EditControlTypeId, name="partial", fail_on="CurrentAutomationId")
    good = Element(T.UIA_ButtonControlTypeId, name="OK")
    _, _, inspector = backend(bad_name, bad_id, good)
    inspection = inspector.inspect(71, 17)
    assert [control.name for control in inspection.controls] == ["OK"]
    assert "partial" not in str(inspection)


def test_all_bad_children_with_valid_root_return_successful_empty_result():
    bad = [FaultyElement(T.UIA_ButtonControlTypeId, fail_on="CurrentName") for _ in range(3)]
    _, _, inspector = backend(*bad)
    inspection = inspector.inspect(71, 17)
    assert inspection == UIInspection((), False)
    _, _, registry = tool_harness(inspection=inspection)
    result = registry.execute("ui_inspect", {"query": "Notepad"})
    assert result.success
    assert result.message.startswith("No supported interactive controls")


def test_top_level_identity_change_after_inspection_is_still_failure():
    controller = FakeWindowController((TARGET,))
    class ChangingInspector:
        def inspect(self, handle, process_id):
            controller.windows = [replace(TARGET, title="Changed")]
            return UIInspection((UIElementInfo(
                "button", "Save", "", True, True, False, False, (),
            ),), False)
    registry = make_registry(FakeLauncher(), window_controller=controller,
                             ui_inspector=ChangingInspector())
    result = registry.execute("ui_inspect", {"query": "Notepad"})
    assert not result.success and "changed or is no longer available" in result.message
    assert "Save" not in result.message


def test_only_supported_descendants_inside_prepared_root_in_deterministic_order():
    outer = Element(T.UIA_WindowControlTypeId, handle=99)
    wrong_root = Element(T.UIA_ButtonControlTypeId, parent=outer, name="Outside")
    wrong_pid = Element(T.UIA_ButtonControlTypeId, pid=18, name="Wrong process")
    decorative_text = Element(T.UIA_TextControlTypeId, name="Decoration")
    decorative_pane = Element(T.UIA_PaneControlTypeId, name="Pane")
    button = Element(T.UIA_ButtonControlTypeId, name="Save")
    edit = Element(T.UIA_EditControlTypeId, name="Editor")
    root, automation, inspector = backend(decorative_text, wrong_root, button,
                                           decorative_pane, wrong_pid, edit)
    result = inspector.inspect(71, 17)
    assert [(c.control_type, c.name) for c in result.controls] == [
        ("button", "Save"), ("edit", "Editor")]
    assert root.searches and root.searches[0][0] == T.TreeScope_Descendants
    assert automation.handles == [71]


def test_every_supported_control_type_and_capability_is_descriptive_only():
    children = [Element(getattr(T, f"UIA_{raw}ControlTypeId"), name=raw)
                for raw, _ in _CONTROL_TYPES]
    children[0].patterns = {getattr(T, f"UIA_Is{raw}PatternAvailablePropertyId")
                            for raw, _ in _PATTERNS}
    children[1].patterns = {T.UIA_IsValuePatternAvailablePropertyId}
    _, _, inspector = backend(*children)
    result = inspector.inspect(71, 17)
    assert [c.control_type for c in result.controls] == [friendly for _, friendly in _CONTROL_TYPES]
    assert result.controls[0].capabilities == tuple(label for _, label in _PATTERNS)
    assert result.controls[1].capabilities == ("edit",)
    assert not result.truncated


def test_first_40_controls_returned_and_truncation_reported():
    children = [Element(T.UIA_ButtonControlTypeId, name=f"Button {i}") for i in range(43)]
    _, _, inspector = backend(*children)
    inspection = inspector.inspect(71, 17)
    assert len(inspection.controls) == MAX_CONTROLS
    assert inspection.controls[-1].name == "Button 39"
    assert inspection.truncated
    _, _, registry = tool_harness(inspection=inspection)
    result = registry.execute("ui_inspect", {"query": "Notepad"})
    assert result.success and "truncated" in result.message
    assert len(result.message) < 10000 and len(result.details["controls"]) == 40


def test_names_ids_and_password_are_private_and_bounded():
    long = "X" * 300
    visible = Element(T.UIA_EditControlTypeId, name=long, automation_id=long)
    password = Element(T.UIA_EditControlTypeId, name="secret", automation_id="private-id",
                       password=True, patterns=(T.UIA_IsValuePatternAvailablePropertyId,))
    _, _, inspector = backend(visible, password)
    inspection = inspector.inspect(71, 17)
    assert len(inspection.controls[0].name) == 120
    assert len(inspection.controls[0].automation_id) == 80
    assert inspection.controls[1].name == "password field"
    assert inspection.controls[1].automation_id == ""
    assert "edit" not in inspection.controls[1].capabilities
    _, _, registry = tool_harness(inspection=inspection)
    result = registry.execute("ui_inspect", {"query": "Notepad"})
    assert "secret" not in str(result) and "private-id" not in str(result)
    assert "handle" not in result.details and "process_id" not in result.details


def test_no_value_or_text_contents_or_mutating_api_is_read():
    child = Element(T.UIA_DocumentControlTypeId, patterns=(
        T.UIA_IsTextPatternAvailablePropertyId,
        T.UIA_IsTextEditPatternAvailablePropertyId,
    ))
    _, _, inspector = backend(child)
    inspection = inspector.inspect(71, 17)
    assert inspection.controls[0].capabilities == ("edit",)
    assert T.UIA_IsTextPatternAvailablePropertyId not in child.reads  # content pattern is unnecessary


def test_schema_is_safe_and_exposes_no_child_ids_coordinates_or_mouse():
    _, _, registry = tool_harness()
    schema = next(s for s in registry.schemas() if s["name"] == "ui_inspect")
    assert schema["parameters"]["properties"].keys() == {"query"}
    assert schema["parameters"]["required"] == ["query"]
    assert registry.execute("ui_inspect", {"query": "Notepad", "handle": 71}).success is False
    assert all(s["name"] not in {"click", "move_mouse", "scroll"} for s in registry.schemas())


def test_provider_examples_cover_greek_greeklish_english_and_explicit_query():
    for phrase in ("Τι controls έχει το Notepad", "Τι κουμπιά έχει το Chrome",
                   "παράθυρο Bookish", "Show me the controls in VS Code",
                   "Ti koumpia exei to Notepad"):
        assert phrase in _INSTRUCTIONS
    assert "Do not infer 'this window' or 'here'" in _INSTRUCTIONS


def test_bootstrap_injects_inspector_and_routes_provider_action():
    controller = FakeWindowController((TARGET,))
    inspector = FakeUIInspector(UIInspection((UIElementInfo(
        "button", "Settings", "SettingsButton", True, True, False, False, ("invoke",)
    ),), False))

    class Provider:
        def resolve(self, request):
            return IntentResult.tool_action("ui_inspect", {"query": "Notepad"})

    assistant = build_assistant(settings=Settings(openai_api_key=None),
                                intent_provider=Provider(), window_controller=controller,
                                ui_inspector=inspector)
    result = assistant.handle("Τι κουμπιά έχει το Notepad;")
    assert result.success and "Settings" in result.message
    assert inspector.calls == [(71, 17)] and controller.focused_handles == []


def test_true_condition_and_missing_optional_constant(monkeypatch):
    children = [Element(T.UIA_ButtonControlTypeId, name="Save"),
                Element(T.UIA_TextControlTypeId, name="Decoration"),
                Element(T.UIA_DocumentControlTypeId, name="Editor")]
    root, _, inspector = backend(*children)
    monkeypatch.delattr(T, "UIA_SpinnerControlTypeId")
    result = inspector.inspect(71, 17)
    assert root.searches == [(T.TreeScope_Descendants, "true-condition")]
    assert [c.name for c in result.controls] == ["Save", "Editor"]


@pytest.mark.parametrize("count", [MAX_SCANNED_ELEMENTS, MAX_SCANNED_ELEMENTS + 10])
def test_scan_bound_is_independent_of_supported_result_bound(count):
    root, _, inspector = backend()
    decorative = Element(T.UIA_PaneControlTypeId, parent=root)
    button = Element(T.UIA_ButtonControlTypeId, parent=root, name="Save")
    scanned = []
    def get_element(index):
        scanned.append(index)
        return button if index == 0 else decorative
    root.FindAll = lambda scope, condition: SimpleNamespace(Length=count, GetElement=get_element)
    result = inspector.inspect(71, 17)
    assert scanned == list(range(MAX_SCANNED_ELEMENTS))
    assert [c.name for c in result.controls] == ["Save"]
    assert result.truncated


@pytest.mark.parametrize("stage", list(InspectionStage))
def test_hard_failure_stage_is_logged_without_provider_content(stage, caplog):
    root, automation, inspector = backend(Element(T.UIA_ButtonControlTypeId))
    def fail(*args):
        raise OSError("SECRET provider application content")
    if stage is InspectionStage.BACKEND_OPEN:
        inspector._backend.open = fail
    elif stage is InspectionStage.ELEMENT_FROM_HANDLE:
        automation.ElementFromHandle = fail
    elif stage is InspectionStage.ROOT_IDENTITY:
        root.CurrentProcessId = 999
    elif stage is InspectionStage.CONDITION_CREATION:
        automation.CreateTrueCondition = fail
    elif stage is InspectionStage.FIND_ALL:
        root.FindAll = fail
    else:
        original_find = root.FindAll
        def change_identity(*args):
            root.CurrentProcessId = 999
            return original_find(*args)
        root.FindAll = change_identity
    with pytest.raises(UIInspectionError) as error:
        inspector.inspect(71, 17)
    assert error.value.stage is stage
    assert str(error.value) == "The window controls could not be inspected safely."
    records = [r for r in caplog.records if r.name == "desktop_assistant.ui_perception"]
    assert len(records) == 1
    expected_class = "UIInspectionError" if stage in {
        InspectionStage.ROOT_IDENTITY, InspectionStage.FINAL_ROOT_IDENTITY,
    } else "OSError"
    assert records[0].getMessage() == f"stage={stage.value} exception={expected_class}"
    assert records[0].exc_info is None
    assert "SECRET" not in caplog.text and "SECRET" not in str(error.value)


def test_inspector_contains_no_content_or_mutation_api_calls():
    import inspect
    source = inspect.getsource(WindowsUIInspector)
    for forbidden in ("SetFocus", "SetForegroundWindow", "Invoke(", "SetValue",
                      "Select(", "Toggle(", "Expand(", "Scroll(", "SendInput",
                      "RuntimeId", "CurrentValue", "DocumentRange", "Clipboard"):
        assert forbidden not in source
