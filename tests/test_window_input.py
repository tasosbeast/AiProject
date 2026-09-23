from __future__ import annotations

import ctypes
from dataclasses import FrozenInstanceError, replace
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import FakeInputController, FakeLauncher, FakeWindowController, make_registry
from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog
from desktop_assistant.confirmation import PreparedAction
from desktop_assistant.intent.openai_provider import OpenAIIntentProvider
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolArguments, ToolResult
from desktop_assistant.process_control import WindowInfo, WindowsWindowController
from desktop_assistant.router import CommandRouter
from desktop_assistant.window_input import (
    InputAction, InputError, MAX_TEXT_LENGTH, PreparedWindowInput, WindowInputTool,
    WindowsInputController, _Input, _KeyboardInput,
)
from test_openai_provider import FakeClient, function_call


TARGET = WindowInfo(12345, 67890, "Untitled - Notepad", "notepad.exe")


@pytest.fixture(autouse=True)
def no_real_keyboard(monkeypatch):
    original = WindowsInputController._sender

    def require_fake(self):
        assert self._send_input is not None, "Tests must inject SendInput; never type into the desktop"
        return original(self)

    monkeypatch.setattr(WindowsInputController, "_sender", require_fake)


class Windows(FakeWindowController):
    def __init__(self, target=TARGET):
        super().__init__((target,))
        self.events = []
        self.snapshots = 0
        self.wrong_foreground = False

    def visible_windows(self):
        self.snapshots += 1
        return super().visible_windows()

    def restore_window(self, handle):
        self.events.append("restore")
        self.windows = [replace(w, minimized=False) if w.handle == handle else w for w in self.windows]
        # ShowWindow returns previous visibility, not restoration success.
        return False

    def set_foreground_window(self, handle):
        self.events.append("focus")
        if not self.focus_succeeds:
            return False
        self.active_window = replace(TARGET, handle=777) if self.wrong_foreground else self.get_window_info(handle)
        return True


class Sender:
    def __init__(self, counts=()):
        self.calls = []
        self.counts = iter(counts)

    def __call__(self, count, records, size):
        assert size == ctypes.sizeof(_Input)
        assert all(r.type == 1 for r in records)
        self.calls.append([(r.data.ki.wVk, r.data.ki.wScan, r.data.ki.dwFlags) for r in records])
        result = next(self.counts, count)
        if isinstance(result, Exception):
            raise result
        return result


def setup(target=TARGET):
    windows = Windows(target)
    sender = Sender()
    inputs = WindowsInputController(sender)
    registry = make_registry(FakeLauncher(), window_controller=windows, input_controller=inputs)
    return windows, sender, registry


def prepared(registry, **overrides):
    args = {"query": "Notepad", "action": "type_text", "value": " Γειά σου 🌍\n"}
    args.update(overrides)
    result = registry.prepare("window_input", args)
    assert isinstance(result, PreparedAction)
    return result


def test_preparation_freezes_exact_target_action_and_unmodified_text():
    windows, sender, registry = setup()
    action = prepared(registry)
    payload = action.execution_value
    assert payload == PreparedWindowInput(TARGET.handle, TARGET.process_id, TARGET.title,
                                         TARGET.executable_name, InputAction.TYPE_TEXT, " Γειά σου 🌍\n")
    assert action.risk_level is RiskLevel.SENSITIVE
    assert windows.snapshots == 1
    assert sender.calls == []
    with pytest.raises(FrozenInstanceError):
        payload.value = "other"
    with pytest.raises(FrozenInstanceError):
        action.execution_value = None
    assert "Γειά" not in repr(payload)


def test_confirmation_is_bounded_and_contains_target_and_exact_action():
    _, sender, registry = setup()
    action = prepared(registry, value="a" * MAX_TEXT_LENGTH)
    request = registry.dispatch_prepared(action)
    assert isinstance(request, ConfirmationRequest)
    assert "Target:\nUntitled - Notepad — notepad.exe" in request.summary
    assert "Action:\ntype_text" in request.summary
    assert "Text:\n" in request.summary
    assert "2000 characters" in request.summary
    assert len(request.summary) < 400
    assert str(TARGET.handle) not in request.summary
    assert str(TARGET.process_id) not in request.summary
    assert sender.calls == []


@pytest.mark.parametrize("change", ["gone", "pid", "exe", "title", "focus", "foreground", "restoration"])
def test_changed_or_unfocused_target_sends_zero_events(change):
    windows, sender, registry = setup()
    action = prepared(registry)
    if change == "gone":
        windows.valid_handles.clear()
    elif change == "pid":
        windows.windows[0] = replace(TARGET, process_id=42)
    elif change == "exe":
        windows.windows[0] = replace(TARGET, executable_name="other.exe")
    elif change == "title":
        windows.windows[0] = replace(TARGET, title="Other document - Notepad")
    elif change == "focus":
        windows.focus_succeeds = False
    elif change == "foreground":
        windows.wrong_foreground = True
    else:
        windows.windows[0] = replace(TARGET, minimized=True)
        windows.restore_window = lambda handle: True
    request = registry.dispatch_prepared(action)
    result = registry.confirm(request.confirmation_id)
    assert not result.success
    assert sender.calls == []
    assert windows.snapshots == 1  # Confirmation never resolves a new window.


def test_full_title_identity_detects_changes_beyond_display_preview():
    target = replace(TARGET, title="a" * 120 + "document 1")
    windows, sender, registry = setup(target)
    action = prepared(registry)
    windows.windows[0] = replace(target, title="a" * 120 + "document 2")
    request = registry.dispatch_prepared(action)
    assert not registry.confirm(request.confirmation_id).success
    assert sender.calls == []


def test_restores_before_focus_then_types_and_enters():
    windows, sender, registry = setup(replace(TARGET, minimized=True))
    action = prepared(registry, action="type_text_and_enter", value="γειά")
    request = registry.dispatch_prepared(action)
    result = registry.confirm(request.confirmation_id)
    assert result.success
    assert windows.events == ["restore", "focus"]
    assert sender.calls[0][-2:] == [(0x0D, 0, 0), (0x0D, 0, 2)]
    assert result.message == "Typed 4 characters into Untitled - Notepad. Pressed Enter."
    assert "γειά" not in str(result)


def test_recheck_after_focus_catches_identity_change():
    windows, sender, registry = setup()
    action = prepared(registry)

    def focus(handle):
        windows.active_window = replace(TARGET, executable_name="changed.exe")
        return True

    windows.set_foreground_window = focus
    request = registry.dispatch_prepared(action)
    assert not registry.confirm(request.confirmation_id).success
    assert sender.calls == []


@pytest.mark.parametrize("query,windows", [
    ("Report", (replace(TARGET, title="Report one"), replace(TARGET, handle=2, title="Report two"))),
    ("Report", (replace(TARGET, title="Report"), replace(TARGET, handle=2, title="Report"))),
    ("Missing", (TARGET,)), ("Notepad", ()),
    ("Notepad", (replace(TARGET, executable_name=""),)),
])
def test_ambiguous_absent_or_unverifiable_target_rejected(query, windows):
    controller = Windows()
    controller.windows = list(windows)
    inputs = FakeInputController()
    tool = WindowInputTool(controller, inputs, AppCatalog())
    result = tool.prepare(ToolArguments.from_mapping({"query": query, "action": "enter"}))
    assert isinstance(result, ToolResult) and not result.success
    assert inputs.calls == []


@pytest.mark.parametrize("arguments", [
    {"action": "type_text"}, {"action": "type_text", "value": ""},
    {"action": "type_text", "value": "a" * (MAX_TEXT_LENGTH + 1)},
    {"action": "type_text", "value": "\ud800"}, {"action": "type_text", "value": None},
    {"action": "enter", "value": ""}, {"action": "ctrl_s", "value": "secret"},
    {"action": "enter", "value": None}, {"action": "Alt+F4"}, {"action": "win_r"},
    {"action": "ctrl_shift_s"}, {"action": 13}, {"action": "enter", "vk": "13"},
    {"action": "enter", "handle": "12345"}, {"action": "enter", "pid": "67890"},
    {"action": "enter", "query": " "}, {"action": "enter", "query": 1},
])
def test_invalid_arguments_rejected_before_snapshot(arguments):
    windows, sender, registry = setup()
    result = registry.execute("window_input", {"query": "Notepad", **arguments})
    assert isinstance(result, ToolResult) and not result.success
    assert windows.snapshots == 0
    assert sender.calls == []


def test_local_schema_has_only_query_action_optional_string_value():
    _, _, registry = setup()
    schema = next(s for s in registry.schemas() if s["name"] == "window_input")
    assert schema["strict"]
    params = schema["parameters"]
    assert set(params["properties"]) == {"query", "action", "value"}
    assert params["required"] == ["query", "action"]
    assert params["properties"]["value"]["type"] == "string"
    assert not params["additionalProperties"]
    assert params["properties"]["action"]["enum"] == [a.value for a in InputAction]
    assert len(InputAction) == 23


@pytest.mark.parametrize("action", ["type_text", "type_text_and_enter"])
def test_text_bounds_accept_exact_max_and_reject_one_more(action):
    _, sender, registry = setup()
    request = registry.execute("window_input", {
        "query": "Notepad", "action": action, "value": "🌍" * MAX_TEXT_LENGTH,
    })
    assert isinstance(request, ConfirmationRequest)
    assert registry.confirm(request.confirmation_id).success
    assert len(sender.calls[0]) == MAX_TEXT_LENGTH * 4 + (2 if action.endswith("enter") else 0)
    result = registry.execute("window_input", {
        "query": "Notepad", "action": action, "value": "🌍" * (MAX_TEXT_LENGTH + 1),
    })
    assert isinstance(result, ToolResult) and not result.success
    assert len(sender.calls) == 1


def test_request_arguments_cannot_change_pending_payload():
    _, sender, registry = setup()
    args = {"query": "Notepad", "action": "type_text", "value": "original"}
    request = registry.execute("window_input", args)
    args.update(query="Chrome", action="type_text_and_enter", value="changed")
    assert registry.confirm(request.confirmation_id).success
    assert sender.calls[0] == [event for c in "original" for event in [(0, ord(c), 4), (0, ord(c), 6)]]


def test_unicode_utf16_units_including_greek_and_surrogate_pair():
    sender = Sender()
    WindowsInputController(sender).send(InputAction.TYPE_TEXT, " Αλφα 🌍\n", lambda: True)
    raw = " Αλφα 🌍\n".encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    assert sender.calls == [[event for unit in units for event in [(0, unit, 4), (0, unit, 6)]]]
    assert ctypes.sizeof(_Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(_KeyboardInput) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


@pytest.mark.parametrize("action,keys", [
    ("enter", [0x0D]), ("escape", [0x1B]), ("tab", [0x09]), ("backspace", [0x08]),
    ("delete", [0x2E]), ("arrow_up", [0x26]), ("arrow_down", [0x28]),
    ("arrow_left", [0x25]), ("arrow_right", [0x27]), ("home", [0x24]),
    ("end", [0x23]), ("page_up", [0x21]), ("page_down", [0x22]),
    ("ctrl_a", [0x11, 0x41]), ("ctrl_c", [0x11, 0x43]), ("ctrl_v", [0x11, 0x56]),
    ("ctrl_s", [0x11, 0x53]), ("ctrl_f", [0x11, 0x46]), ("ctrl_l", [0x11, 0x4C]),
    ("ctrl_z", [0x11, 0x5A]), ("ctrl_y", [0x11, 0x59]),
])
def test_fixed_keys_and_modifiers_are_released_in_reverse_order(action, keys):
    sender = Sender()
    WindowsInputController(sender).send(InputAction(action), None, lambda: True)
    events = sender.calls[0]
    assert [event[0] for event in events] == keys + list(reversed(keys))
    assert all(not flags & 2 for _, _, flags in events[:len(keys)])
    assert all(flags & 2 for _, _, flags in events[len(keys):])
    assert all(scan == 0 for _, scan, _ in events)
    if action in {"delete", "arrow_up", "arrow_down", "arrow_left", "arrow_right", "home", "end", "page_up", "page_down"}:
        assert all(flags & 1 for _, _, flags in events)


@pytest.mark.parametrize("inserted,release", [(0, []), (1, [(0x11, 0, 2)]),
    (2, [(0x53, 0, 2), (0x11, 0, 2)]), (3, [(0x11, 0, 2)])])
def test_partial_send_releases_only_pressed_keys_without_replaying_action(inserted, release):
    sender = Sender((inserted,))
    with pytest.raises(InputError, match="partial"):
        WindowsInputController(sender).send(InputAction.CTRL_S, None, lambda: True)
    assert sender.calls[1:] == ([release] if release else [])


def test_partial_unicode_releases_pending_unit():
    sender = Sender((1,))
    with pytest.raises(InputError):
        WindowsInputController(sender).send(InputAction.TYPE_TEXT, "α", lambda: True)
    assert sender.calls[1] == [(0, ord("α"), 6)]


def test_sender_exception_releases_keys_and_does_not_echo_exception_text():
    sender = Sender((RuntimeError("secret text"),))
    with pytest.raises(InputError, match="delivery could not be verified") as exc:
        WindowsInputController(sender).send(InputAction.CTRL_S, None, lambda: True)
    assert "secret" not in str(exc.value)
    assert sender.calls[1] == [(0x53, 0, 2), (0x11, 0, 2)]


def test_failed_cleanup_reported():
    sender = Sender((1, 0))
    with pytest.raises(InputError, match="release could not be verified"):
        WindowsInputController(sender).send(InputAction.CTRL_S, None, lambda: True)


def test_final_guard_after_allocation_sends_zero_events(monkeypatch):
    sender = Sender()
    inputs = WindowsInputController(sender)
    ready = False
    native = inputs._native

    def allocated(events):
        nonlocal ready
        ready = True
        return native(events)

    monkeypatch.setattr(inputs, "_native", allocated)

    def verify():
        assert ready
        return False

    with pytest.raises(InputError, match="No input sent"):
        inputs.send(InputAction.CTRL_S, None, verify)
    assert sender.calls == []


def test_no_raw_key_names_allowed_at_backend():
    sender = Sender()
    with pytest.raises(ValueError):
        WindowsInputController(sender).send("ctrl_s", None, lambda: True)
    assert sender.calls == []


@pytest.mark.parametrize("cancel", [False, True])
def test_confirmation_consumes_exact_payload_once_without_provider_or_rematch(cancel):
    windows, sender, registry = setup()
    arguments = {"query": "Notepad", "action": "type_text", "value": "hello"}
    client = FakeClient(SimpleNamespace(output=[function_call("window_input", json.dumps(arguments))]))
    provider = OpenAIIntentProvider(api_key="fake", model="fake", tool_schemas=registry.schemas(), client=client)
    assistant = Assistant(CommandRouter(registry, AppCatalog()), registry, provider)
    response = assistant.handle("Write hello in Notepad.")
    assert response.confirmation is not None
    assert sender.calls == []
    # Another alias match appearing first must not replace the authorized target.
    windows.windows.insert(0, replace(TARGET, handle=42, title="Another Notepad"))
    confirmation_id = response.confirmation.confirmation_id
    result = assistant.cancel(confirmation_id) if cancel else assistant.confirm(confirmation_id)
    assert result.success
    assert len(client.responses.calls) == 1
    assert windows.snapshots == 1
    assert len(sender.calls) == (0 if cancel else 1)
    if not cancel:
        assert windows.active_window.handle == TARGET.handle
        assert sender.calls[0] == [event for c in "hello" for event in [(0, ord(c), 4), (0, ord(c), 6)]]
    assert not assistant.confirm(confirmation_id).success


@pytest.mark.parametrize("utterance,args", [
    ("Γράψε hello world στο Notepad.", {"query": "Notepad", "action": "type_text", "value": "hello world"}),
    ("Γράψε hello και πάτα Enter στο Notepad.", {"query": "Notepad", "action": "type_text_and_enter", "value": "hello"}),
    ("Πάτα Ctrl+S στο VS Code.", {"query": "VS Code", "action": "ctrl_s", "value": None}),
    ("Πάτα Enter στο Bookish.", {"query": "Bookish", "action": "enter", "value": None}),
    ("Press Ctrl+L in Chrome.", {"query": "Chrome", "action": "ctrl_l", "value": None}),
    ("Write hello in Notepad.", {"query": "Notepad", "action": "type_text", "value": "hello"}),
    ("Grapse hello sto Notepad.", {"query": "Notepad", "action": "type_text", "value": "hello"}),
    ("Πάτα Ctrl+S in VS Code.", {"query": "VS Code", "action": "ctrl_s", "value": None}),
])
def test_provider_examples_and_mock_routing(utterance, args):
    _, _, registry = setup()
    client = FakeClient(SimpleNamespace(output=[function_call("window_input", json.dumps(args))]))
    provider = OpenAIIntentProvider(api_key="fake", model="fake", tool_schemas=registry.schemas(), client=client)
    result = provider.resolve(utterance)
    assert result.action.tool_name == "window_input"
    assert result.action.arguments == {key: value for key, value in args.items() if value is not None}
    call = client.responses.calls[0]
    assert utterance in call["instructions"]
    assert call["input"] == utterance
    tool = next(t for t in call["tools"] if t["name"] == "window_input")
    assert tool["parameters"]["properties"]["value"]["type"] == ["string", "null"]
    assert tool["parameters"]["required"] == ["query", "action", "value"]


def test_plan_transport_handles_optional_value_and_keeps_local_schema_unchanged():
    _, _, registry = setup()
    schemas = registry.schemas()
    actions = [{"tool_name": "window_input", "arguments": {"query": "Notepad", "action": "enter", "value": None}},
               {"tool_name": "window_input", "arguments": {"query": "Notepad", "action": "type_text", "value": "test"}}]
    client = FakeClient(SimpleNamespace(output=[function_call("propose_action_plan", json.dumps({"actions": actions}))]))
    provider = OpenAIIntentProvider(api_key="fake", model="fake", tool_schemas=schemas, client=client)
    result = provider.resolve("Press Enter and write test in Notepad.")
    assert result.plan.actions[0].arguments == {"query": "Notepad", "action": "enter"}
    assert result.plan.actions[1].arguments["value"] == "test"
    assert schemas == registry.schemas()
    variants = provider.plan_schema["parameters"]["properties"]["actions"]["items"]["anyOf"]
    tool = next(v for v in variants if v["properties"]["tool_name"]["enum"] == ["window_input"])
    assert tool["properties"]["arguments"]["required"] == ["query", "action", "value"]


def test_results_and_logs_do_not_contain_typed_text(caplog):
    _, _, registry = setup()
    secret = "SENSITIVE_TEXT_" * 30
    with caplog.at_level(logging.DEBUG):
        request = registry.execute("window_input", {"query": "Notepad", "action": "type_text", "value": secret})
        result = registry.confirm(request.confirmation_id)
    assert result.success
    assert "SENSITIVE_TEXT_" not in caplog.text
    assert "SENSITIVE_TEXT_" not in str(result)


def test_window_controller_exact_identity_delegates_to_injected_api():
    api = SimpleNamespace(get_window_info=lambda handle: TARGET if handle == TARGET.handle else None)
    controller = WindowsWindowController(api)
    assert controller.get_window_info(TARGET.handle) == TARGET
    assert controller.get_window_info(999) is None


def test_native_identity_keeps_full_title_and_rechecks_foreground():
    from ctypes import wintypes
    from desktop_assistant.process_control import _Win32ProcessApi, _ProcessEntry

    title = "X" * 120 + "original document"

    def get_text(handle, buffer, size):
        buffer.value = title
        return len(title)

    def get_pid(handle, pointer):
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = TARGET.process_id
        return 1

    handles = iter((TARGET.handle, 777))
    api = object.__new__(_Win32ProcessApi)
    api._user32 = SimpleNamespace(
        IsWindow=lambda handle: handle == TARGET.handle,
        GetWindowTextLengthW=lambda handle: len(title), GetWindowTextW=get_text,
        GetWindowThreadProcessId=get_pid, IsIconic=lambda handle: False,
        GetForegroundWindow=lambda: next(handles),
    )
    api.processes = lambda: (_ProcessEntry(TARGET.process_id, TARGET.executable_name),)
    info = api.get_window_info(TARGET.handle)
    assert info == replace(TARGET, title=title)
    assert api.get_foreground_window() is None


def test_production_bootstrap_registers_injected_keyboard_tool():
    from desktop_assistant.bootstrap import build_assistant
    from desktop_assistant.config import Settings
    from desktop_assistant.intent.models import IntentResult
    from test_windows import FakeProvider
    from conftest import FakeMediaController, FakeProcessController, FakeSystemStatusCollector

    windows = Windows()
    inputs = FakeInputController()
    provider = FakeProvider(IntentResult.tool_action("window_input", {"query": "Notepad", "action": "ctrl_s"}))
    assistant = build_assistant(
        settings=Settings(openai_api_key=None), launcher=FakeLauncher(), intent_provider=provider,
        process_controller=FakeProcessController(), media_controller=FakeMediaController(),
        system_status_collector=FakeSystemStatusCollector(), window_controller=windows,
        input_controller=inputs,
    )
    response = assistant.handle("Πάτα Ctrl+S στο Notepad.")
    assert response.confirmation is not None
    assert inputs.calls == []
    assert assistant.confirm(response.confirmation.confirmation_id).success
    assert inputs.calls == [(InputAction.CTRL_S, None)]
    assert len(provider.calls) == 1
