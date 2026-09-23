from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import json

import pytest

from conftest import (FakeEditableControlResolver, FakeInputController, FakeLauncher,
                     make_registry)
from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog
from desktop_assistant.editable_controls import EditableControlError, WindowsEditableControlResolver
from desktop_assistant.intent.models import IntentResult
from desktop_assistant.models import ConfirmationRequest, ToolResult
from desktop_assistant.router import CommandRouter
from desktop_assistant.window_input import InputAction, InputError
from test_window_input import TARGET, Windows


TYPES = SimpleNamespace(
    UIA_ControlTypePropertyId=30003, UIA_DocumentControlTypeId=50030,
    UIA_EditControlTypeId=50004, UIA_WindowControlTypeId=50032,
    UIA_ValuePatternId=10002, UIA_TextPatternId=10014, UIA_TextEditPatternId=10032,
    UIA_IsReadOnlyAttributeId=40015, TreeScope_Descendants=4,
    IUIAutomationValuePattern="value", IUIAutomationTextPattern="text",
    IUIAutomationTextEditPattern="textedit",
)


class Pattern:
    def __init__(self, value):
        self.value = value

    def QueryInterface(self, interface):
        return self.value


class Element:
    def __init__(self, kind, *, parent=None, pid=TARGET.process_id,
                 handle=0, enabled=True, focusable=True, readonly=False,
                 text_readonly=None, password=False):
        self.parent = parent
        self.CurrentControlType = kind
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = handle
        self.CurrentIsEnabled = enabled
        self.CurrentIsKeyboardFocusable = focusable
        self.CurrentIsPassword = password
        self.CurrentHasKeyboardFocus = False
        self.patterns = {}
        self.focus_error = False
        self.on_focus = None
        if readonly is not None:
            self.patterns[TYPES.UIA_ValuePatternId] = SimpleNamespace(CurrentIsReadOnly=readonly)
        if text_readonly is not None:
            self.patterns[TYPES.UIA_TextPatternId] = SimpleNamespace(
                DocumentRange=SimpleNamespace(GetAttributeValue=lambda attr: text_readonly)
            )

    def GetCurrentPattern(self, pattern_id):
        value = self.patterns.get(pattern_id)
        return Pattern(value) if value is not None else None

    def SetFocus(self):
        if self.focus_error:
            raise OSError("fake UIA focus failure")
        if self.on_focus is not None:
            self.on_focus(self)


class Array:
    def __init__(self, elements):
        self.elements = elements
        self.Length = len(elements)

    def GetElement(self, index):
        return self.elements[index]


class Root(Element):
    def __init__(self, elements=()):
        super().__init__(TYPES.UIA_WindowControlTypeId, handle=TARGET.handle)
        self.elements = list(elements)
        self.searches = []

    def FindAll(self, scope, condition):
        self.searches.append((scope, condition))
        return Array(self.elements)


class Automation:
    def __init__(self, root):
        self.root = root
        self.focused = None
        self.handles = []
        self.RawViewWalker = SimpleNamespace(GetParentElement=lambda element: element.parent)

    def ElementFromHandle(self, handle):
        self.handles.append(handle)
        return self.root

    def CreatePropertyCondition(self, property_id, value):
        return (property_id, value)

    def CreateOrCondition(self, left, right):
        return (left, right)

    def CompareElements(self, left, right):
        return left is right

    def GetFocusedElement(self):
        return self.focused


class Backend:
    def __init__(self, automation):
        self.automation = automation
        self.opens = 0
        self.closes = 0

    @contextmanager
    def open(self):
        self.opens += 1
        try:
            yield self.automation, TYPES
        finally:
            self.closes += 1


def harness(*, kind=TYPES.UIA_DocumentControlTypeId, readonly=False,
            text_readonly=None, parent=None, pid=TARGET.process_id):
    root = Root()
    editor = Element(kind, parent=parent or root, pid=pid, readonly=readonly,
                     text_readonly=text_readonly)
    root.elements = [editor]
    automation = Automation(root)

    def focus(element):
        automation.focused = element
        element.CurrentHasKeyboardFocus = True

    editor.on_focus = focus
    backend = Backend(automation)
    resolver = WindowsEditableControlResolver(backend, focus_timeout_seconds=0)
    windows = Windows()
    inputs = FakeInputController()
    registry = make_registry(FakeLauncher(), window_controller=windows,
                             input_controller=inputs, editable_control_resolver=resolver)
    return windows, inputs, registry, root, editor, automation, backend


def execute(registry, *, action="type_text", value="Καλημέρα Τάσο"):
    args = {"query": "Notepad", "action": action}
    if value is not None:
        args["value"] = value
    request = registry.execute("window_input", args)
    assert isinstance(request, ConfirmationRequest)
    return registry.confirm(request.confirmation_id)


@pytest.mark.parametrize("action", ["type_text", "type_text_and_enter"])
def test_one_document_is_focused_before_entire_text_action(action):
    windows, inputs, registry, root, editor, automation, backend = harness()
    result = execute(registry, action=action)
    assert result.success
    assert inputs.calls == [(InputAction(action), "Καλημέρα Τάσο")]
    assert automation.handles == [TARGET.handle]
    assert root.searches == [(4, ((30003, 50030), (30003, 50004)))]
    assert editor.CurrentHasKeyboardFocus
    assert windows.active_window.handle == TARGET.handle
    assert backend.opens == backend.closes == 1


def test_no_editable_child_fails_without_input():
    _, inputs, registry, root, _, _, backend = harness()
    root.elements = []
    result = execute(registry)
    assert not result.success
    assert result.message == "No editable text control could be identified in Untitled - Notepad."
    assert inputs.calls == []
    assert backend.opens == backend.closes == 1


def test_multiple_equal_editors_are_ambiguous_and_neither_is_focused():
    _, inputs, registry, root, editor, automation, _ = harness()
    second = Element(TYPES.UIA_DocumentControlTypeId, parent=root)
    root.elements.append(second)
    result = execute(registry)
    assert not result.success and "Multiple equally suitable" in result.message
    assert inputs.calls == []
    assert automation.focused is None
    assert not editor.CurrentHasKeyboardFocus


@pytest.mark.parametrize("fault", ["pid", "other_window", "focus_failed", "focused_wrong_pid",
                                  "focused_wrong_window", "top_level_changed", "focus_moves_away"])
def test_wrong_identity_or_focus_sends_zero_input(fault):
    windows, inputs, registry, root, editor, automation, _ = harness()
    if fault == "pid":
        editor.CurrentProcessId = 5
    elif fault == "other_window":
        editor.parent = Element(TYPES.UIA_WindowControlTypeId, handle=999, parent=root)
    elif fault == "focus_failed":
        editor.focus_error = True
    elif fault in {"focused_wrong_pid", "focused_wrong_window"}:
        alien = Element(TYPES.UIA_EditControlTypeId, parent=root if fault == "focused_wrong_pid" else None,
                        pid=5 if fault == "focused_wrong_pid" else TARGET.process_id)
        editor.on_focus = lambda element: setattr(automation, "focused", alien)
        alien.CurrentHasKeyboardFocus = True
    elif fault == "top_level_changed":
        editor.on_focus = lambda element: setattr(root, "CurrentNativeWindowHandle", 999)
    else:
        editor.on_focus = lambda element: setattr(windows, "active_window", replace(TARGET, handle=999))
    result = execute(registry)
    assert not result.success
    assert inputs.calls == []


def test_read_only_password_or_disabled_editors_are_rejected():
    for alteration in ("readonly", "password", "disabled", "unfocusable"):
        _, inputs, registry, _, editor, _, _ = harness()
        if alteration == "readonly":
            editor.patterns[TYPES.UIA_ValuePatternId].CurrentIsReadOnly = True
        elif alteration == "password":
            editor.CurrentIsPassword = True
        elif alteration == "disabled":
            editor.CurrentIsEnabled = False
        else:
            editor.CurrentIsKeyboardFocusable = False
        result = execute(registry)
        assert not result.success
        assert inputs.calls == []


def test_text_pattern_without_value_is_accepted_only_when_editable():
    _, inputs, registry, _, editor, _, _ = harness(readonly=None, text_readonly=False)
    assert execute(registry).success
    assert len(inputs.calls) == 1
    _, inputs, registry, _, editor, _, _ = harness(readonly=None, text_readonly=True)
    assert not execute(registry).success
    assert inputs.calls == []


def test_text_edit_pattern_supports_blank_document_with_unknown_readonly():
    _, inputs, registry, _, editor, _, _ = harness(readonly=None, text_readonly="unsupported")
    editor.patterns[TYPES.UIA_TextEditPatternId] = object()
    assert execute(registry).success
    assert len(inputs.calls) == 1


def test_document_preferred_over_single_edit_and_equal_documents_fail():
    _, inputs, registry, root, document, automation, _ = harness()
    edit = Element(TYPES.UIA_EditControlTypeId, parent=root)
    root.elements.append(edit)
    assert execute(registry).success
    assert automation.focused is document
    assert len(inputs.calls) == 1


def test_inner_focused_element_must_remain_inside_editor():
    _, inputs, registry, root, editor, automation, _ = harness()
    inner = Element(TYPES.UIA_EditControlTypeId, parent=editor)
    inner.CurrentHasKeyboardFocus = True
    editor.on_focus = lambda element: setattr(automation, "focused", inner)
    assert execute(registry).success
    assert len(inputs.calls) == 1


def test_late_child_focus_change_is_caught_by_send_guard():
    _, inputs, registry, _, editor, automation, _ = harness()

    class LateInput(FakeInputController):
        def send(self, action, value, verify_target):
            automation.focused = None
            super().send(action, value, verify_target)

    late = LateInput()
    registry = make_registry(FakeLauncher(), window_controller=Windows(), input_controller=late,
                             editable_control_resolver=WindowsEditableControlResolver(Backend(automation)))
    assert not execute(registry).success
    assert late.calls == []


def test_send_failure_passes_through_com_scope_with_original_failure_message():
    windows, _, _, _, _, automation, backend = harness()

    class FailingInput(FakeInputController):
        def send(self, action, value, verify_target):
            assert verify_target()
            raise InputError("Windows did not accept all keyboard events; input may be partial.")

    inputs = FailingInput()
    registry = make_registry(FakeLauncher(), window_controller=windows,
                             input_controller=inputs,
                             editable_control_resolver=WindowsEditableControlResolver(backend))
    result = execute(registry)
    assert not result.success
    assert "input may be partial" in result.message
    assert inputs.calls == []
    assert backend.opens == backend.closes == 1


def test_text_actions_resolve_editor_but_non_text_actions_do_not():
    windows, inputs, registry, _, _, _, backend = harness()
    for action in ("ctrl_s", "ctrl_f", "ctrl_l", "enter", "arrow_left"):
        assert execute(registry, action=action, value=None).success
    assert [a for a, _ in inputs.calls] == [InputAction.CTRL_S, InputAction.CTRL_F,
                                            InputAction.CTRL_L, InputAction.ENTER,
                                            InputAction.ARROW_LEFT]
    assert backend.opens == 0
    assert windows.snapshots == 5


def test_cancel_mutates_no_editable_focus_and_sends_no_input():
    _, inputs, registry, _, editor, automation, backend = harness()
    request = registry.execute("window_input", {"query": "Notepad", "action": "type_text", "value": "abc"})
    assert isinstance(request, ConfirmationRequest)
    assert registry.cancel(request.confirmation_id).success
    assert backend.opens == 0
    assert automation.focused is None and not editor.CurrentHasKeyboardFocus
    assert inputs.calls == []


def test_confirm_uses_immutable_top_level_and_no_second_provider_call():
    from test_windows import FakeProvider

    windows, inputs, registry, _, _, _, backend = harness()
    args = {"query": "Notepad", "action": "type_text", "value": "abc"}
    provider = FakeProvider(IntentResult.tool_action("window_input", args))
    assistant = Assistant(CommandRouter(registry, AppCatalog()), registry, provider)
    response = assistant.handle("Γράψε abc στο Notepad.")
    assert response.confirmation is not None
    with pytest.raises(FrozenInstanceError):
        registry._confirmations._pending.action.execution_value.handle = 1
    args["value"] = "changed"
    result = assistant.confirm(response.confirmation.confirmation_id)
    assert result.success
    assert inputs.calls == [(InputAction.TYPE_TEXT, "abc")]
    assert len(provider.calls) == 1
    assert windows.snapshots == 1
    assert backend.opens == 1


def test_schema_has_no_child_identity_or_mouse_coordinate_api():
    _, _, registry, _, _, _, _ = harness()
    schema = next(s for s in registry.schemas() if s["name"] == "window_input")
    assert set(schema["parameters"]["properties"]) == {"query", "action", "value"}
    assert all(term not in json.dumps(schema).lower() for term in
               ("child_hwnd", "runtime_id", "coordinate", "mouse", "click"))
