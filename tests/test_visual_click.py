from dataclasses import FrozenInstanceError, fields, replace
import ctypes
import json
from types import SimpleNamespace

import pytest

from conftest import (FakeLauncher, FakeMouseClickController, FakeVisualPerceptionProvider,
                      FakeWindowCaptureBackend, FakeWindowController, make_registry)
from test_visual_perception import TARGET
from test_openai_provider import FakeClient, function_call, make_provider
from desktop_assistant.assistant import Assistant
from desktop_assistant.bootstrap import build_assistant
from desktop_assistant.config import Settings
from desktop_assistant.intent.models import IntentKind, IntentResult, ToolAction
from desktop_assistant.intent.provider import MalformedIntentResponseError
from desktop_assistant.intent.openai_provider import _INSTRUCTIONS, build_plan_tool_schema
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolResult
from desktop_assistant.router import CommandRouter
from desktop_assistant.visual_perception import NormalizedVisualBounds, VisualTargetResult, VisualTargetStatus
from desktop_assistant.visual_click import (
    MouseClickError, PreparedVisualClick, WindowRectangle, WindowsMouseClickController,
    _Input, _click_point,
)


ARGS = {"query": "VS Code", "target": "Search"}


def harness(provider=None):
    windows = FakeWindowController((TARGET,), active_window=TARGET)
    mouse = FakeMouseClickController()
    capture = FakeWindowCaptureBackend()
    vision = provider or FakeVisualPerceptionProvider()
    registry = make_registry(FakeLauncher(), window_controller=windows,
                             mouse_click_controller=mouse, window_capture_backend=capture,
                             visual_perception_provider=vision)
    return registry, windows, mouse, capture, vision


def test_schema_sensitive_only_query_and_target():
    registry, _, mouse, capture, _ = harness()
    schema = next(s for s in registry.schemas() if s["name"] == "visual_click")
    assert set(schema["parameters"]["properties"]) == {"query", "target"}
    assert schema["parameters"]["additionalProperties"] is False
    for key in ("x", "y", "coordinates", "bounds", "button", "click_count", "handle", "pid"):
        assert not registry.execute("visual_click", {**ARGS, key: 1}).success
    assert not capture.calls and not mouse.moves
    prepared = registry.prepare("visual_click", ARGS)
    assert prepared.risk_level is RiskLevel.SENSITIVE


def test_prepare_frozen_payload_and_confirmation_without_private_geometry():
    registry, windows, mouse, capture, vision = harness()
    prepared = registry.prepare("visual_click", ARGS)
    payload = prepared.execution_value
    assert isinstance(payload, PreparedVisualClick)
    assert payload.title == TARGET.title and payload.rectangle == mouse.rectangle
    assert mouse.rect_reads == [TARGET.handle, TARGET.handle]
    assert len(capture.calls) == len(vision.target_calls) == len(vision.refinement_calls) == 1
    assert not mouse.moves and not mouse.clicks and not windows.focused_handles
    assert {f.name for f in fields(payload)} == {
        "handle", "process_id", "title", "executable_name", "target", "label", "description", "bounds", "rectangle",
    }
    with pytest.raises(FrozenInstanceError):
        payload.handle = 1
    with pytest.raises(FrozenInstanceError):
        payload.bounds.left = 0
    request = registry.dispatch_prepared(prepared)
    assert isinstance(request, ConfirmationRequest)
    assert TARGET.title in request.summary and "Code.exe" in request.summary and "Search" in request.summary
    assert "Experimental single left click" in request.summary
    assert request.warning == "Visual targeting is experimental and may click a nearby visible control. Review the target before allowing."
    for secret in ("normalized_bounds", "rectangle", "101", "42", "base64", "confidence"):
        assert secret not in request.summary


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_confirm_never_recaptures_or_calls_provider(cancel, caplog):
    registry, _, mouse, capture, vision = harness()
    request = registry.execute("visual_click", ARGS)
    reads = list(mouse.rect_reads)
    result = registry.cancel(request.confirmation_id) if cancel else registry.confirm(request.confirmation_id)
    assert result.success
    assert len(capture.calls) == len(vision.target_calls) == len(vision.refinement_calls) == 1
    assert mouse.clicks == (0 if cancel else 1)
    assert len(mouse.moves) == (0 if cancel else 1)
    if cancel:
        assert mouse.rect_reads == reads
    else:
        assert mouse.moves == [(158, 258)]
        assert set(result.details) == {"title", "requested_target", "resolved_label"}
        assert result.message == f"Clicked visual target 'Search' in {TARGET.title}."
        assert "normalized_bounds" not in str(result) and "base64" not in str(result)
        assert str(mouse.position) not in caplog.text and str(mouse.position) not in str(result)
        registry.confirm(request.confirmation_id)
        assert mouse.clicks == 1


@pytest.mark.parametrize("status", [VisualTargetStatus.NOT_FOUND, VisualTargetStatus.AMBIGUOUS])
def test_prepare_requires_found(status):
    registry, _, mouse, capture, vision = harness(FakeVisualPerceptionProvider(target_result=VisualTargetResult(status)))
    result = registry.execute("visual_click", ARGS)
    assert isinstance(result, ToolResult) and not result.success
    assert not registry.has_pending_confirmation()
    assert len(capture.calls) == 1 and not vision.refinement_calls
    assert not mouse.moves and not mouse.clicks


@pytest.mark.parametrize("stage", ["coarse", "refinement"])
def test_malformed_targeting_produces_no_confirmation(stage):
    registry, _, mouse, _, vision = harness()
    if stage == "coarse":
        vision.locate_target = lambda *args: object()
    else:
        vision.refine_target = lambda *args: object()
    assert not registry.execute("visual_click", ARGS).success
    assert not registry.has_pending_confirmation() and not mouse.moves


def test_rectangle_read_before_and_after_targeting_and_change_rejected():
    registry, _, mouse, capture, vision = harness()
    events = []
    original_read = mouse.get_window_rect
    def read(handle):
        events.append("rect")
        return original_read(handle)
    mouse.get_window_rect = read
    original_capture = capture.capture_window
    def capture_once(handle):
        events.append("capture")
        return original_capture(handle)
    capture.capture_window = capture_once
    original_refine = vision.refine_target
    def refine(*args):
        events.append("refine")
        mouse.rectangle = WindowRectangle(101, 200, 901, 800)
        return original_refine(*args)
    vision.refine_target = refine
    assert not registry.execute("visual_click", ARGS).success
    assert events == ["rect", "capture", "refine", "rect"]
    assert not registry.has_pending_confirmation() and not mouse.moves


@pytest.mark.parametrize("change", ["stale", "pid", "title", "exe", "minimized", "moved", "resized", "focus_failed", "wrong_foreground", "wrong_root"])
def test_execution_guards_send_zero_click_and_no_cursor_move(change):
    registry, windows, mouse, _, _ = harness()
    request = registry.execute("visual_click", ARGS)
    if change == "stale": windows.valid_handles.clear()
    elif change == "pid": windows.windows = [replace(TARGET, process_id=999)]
    elif change == "title": windows.windows = [replace(TARGET, title="changed")]
    elif change == "exe": windows.windows = [replace(TARGET, executable_name="other.exe")]
    elif change == "minimized": windows.windows = [replace(TARGET, minimized=True)]
    elif change == "moved": mouse.rectangle = WindowRectangle(200, 200, 1000, 800)
    elif change == "resized": mouse.rectangle = WindowRectangle(100, 200, 1000, 800)
    elif change == "focus_failed": windows.focus_succeeds = False
    elif change == "wrong_foreground": windows.active_window = replace(TARGET, handle=202)
    elif change == "wrong_root": mouse.root = 202
    result = registry.confirm(request.confirmation_id)
    assert not result.success and mouse.clicks == 0 and not mouse.moves
    assert not windows.restored_handles


@pytest.mark.parametrize("change", ["cursor", "root", "foreground", "rect", "title", "minimized"])
def test_late_change_after_cursor_move_sends_zero_click(change):
    registry, windows, mouse, _, _ = harness()
    request = registry.execute("visual_click", ARGS)
    def changed():
        if change == "cursor": mouse.position = (0, 0)
        elif change == "root": mouse.root = 999
        elif change == "foreground": windows.active_window = None
        elif change == "rect": mouse.rectangle = WindowRectangle(0, 0, 800, 600)
        elif change == "title": windows.windows = [replace(TARGET, title="changed")]
        elif change == "minimized": windows.windows = [replace(TARGET, minimized=True)]
    mouse.on_move = changed
    assert not registry.confirm(request.confirmation_id).success
    assert len(mouse.moves) == 1 and mouse.clicks == 0


def test_point_uses_trusted_center_with_negative_screen_origin():
    assert _click_point(NormalizedVisualBounds(100, 200, 300, 400), WindowRectangle(-1000, -500, 0, 500)) == (-800, -200)
    assert _click_point(NormalizedVisualBounds(0, 0, 1000, 1000), WindowRectangle(0, 0, 3, 3)) == (2, 2)
    with pytest.raises(ValueError):
        _click_point(NormalizedVisualBounds(999, 999, 1000, 1000), WindowRectangle(0, 0, 1, 1))


class NativeMouse:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.batches = []
    def SendInput(self, count, events, size):
        assert size == ctypes.sizeof(_Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.batches.append(tuple(e.data.mi.dwFlags for e in events))
        assert count == len(events)
        assert all(e.type == 0 and e.data.mi.dx == e.data.mi.dy == e.data.mi.mouseData == e.data.mi.time == e.data.mi.dwExtraInfo == 0 for e in events)
        answer = next(self.responses)
        if isinstance(answer, Exception): raise answer
        return answer


@pytest.mark.parametrize("responses,expected", [([2], [(2, 4)]), ([0], [(2, 4)]),
    ([1, 1], [(2, 4), (4,)]), ([1, 0], [(2, 4), (4,)]),
    ([1, OSError("private")], [(2, 4), (4,)]), ([OSError("private"), 1], [(2, 4), (4,)])])
def test_native_single_pair_and_exactly_one_release_cleanup_without_retry(responses, expected):
    native = NativeMouse(responses)
    controller = WindowsMouseClickController(native)
    if responses == [2]: controller.left_click(lambda: True)
    else:
        with pytest.raises(MouseClickError) as error:
            controller.left_click(lambda: True)
        assert "private" not in str(error.value)
        if responses[0] != 0: assert "partial" in str(error.value)
    assert native.batches == expected


def test_native_guard_prevents_sendinput():
    native = NativeMouse([])
    with pytest.raises(MouseClickError): WindowsMouseClickController(native).left_click(lambda: False)
    assert not native.batches


def test_win32_signatures_and_narrow_read_move_api_use_only_owned_constants(monkeypatch):
    from ctypes import wintypes
    calls = []
    position = [0, 0]
    class Function:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)
    def rect(handle, pointer):
        calls.append(("rect", handle))
        pointer._obj.left, pointer._obj.top = -100, 200
        pointer._obj.right, pointer._obj.bottom = 900, 800
        return 1
    def move(x, y):
        calls.append(("move", x, y))
        position[:] = [x, y]
        return 1
    def cursor(pointer):
        pointer._obj.x, pointer._obj.y = position
        return 1
    def window(point):
        calls.append(("point", point.x, point.y))
        return 11
    def ancestor(handle, flag):
        assert (handle, flag) == (11, 2)
        return 101
    api = SimpleNamespace(GetWindowRect=Function(rect), SetCursorPos=Function(move),
        GetCursorPos=Function(cursor), WindowFromPoint=Function(window),
        GetAncestor=Function(ancestor), SendInput=Function(lambda *args: pytest.fail("No input expected")))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: api)
    controller = WindowsMouseClickController()
    assert controller.get_window_rect(101) == WindowRectangle(-100, 200, 900, 800)
    assert controller.root_at_point((50, 60)) == 101
    assert controller.move_cursor((50, 60)) and controller.get_cursor_pos() == (50, 60)
    assert api.GetWindowRect.argtypes == (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    assert api.WindowFromPoint.argtypes == (wintypes.POINT,)
    assert api.GetAncestor.argtypes == (wintypes.HWND, wintypes.UINT)
    assert api.SetCursorPos.argtypes == (ctypes.c_int, ctypes.c_int)
    assert api.GetCursorPos.argtypes == (ctypes.POINTER(wintypes.POINT),)
    assert api.SendInput.argtypes == (ctypes.c_uint32, ctypes.POINTER(_Input), ctypes.c_int)
    assert api.WindowFromPoint.restype == api.GetAncestor.restype == wintypes.HWND
    assert api.SendInput.restype == ctypes.c_uint32
    assert sum(call[0] == "move" for call in calls) == 1


@pytest.mark.parametrize("field,value", [("process_id", 999), ("title", "changed"), ("executable_name", "other.exe")])
def test_same_foreground_handle_with_different_identity_is_rejected(field, value):
    registry, windows, mouse, _, _ = harness()
    request = registry.execute("visual_click", ARGS)
    windows.active_window = replace(TARGET, **{field: value})
    assert not registry.confirm(request.confirmation_id).success
    assert not mouse.moves and mouse.clicks == 0


def test_last_guard_inside_mouse_backend_rechecks_ownership():
    registry, _, mouse, _, _ = harness()
    request = registry.execute("visual_click", ARGS)
    mouse.on_click = lambda: setattr(mouse, "root", 999)
    assert not registry.confirm(request.confirmation_id).success
    assert len(mouse.moves) == 1 and mouse.clicks == 0


def test_foreground_moving_window_is_detected_before_cursor_mutation():
    registry, windows, mouse, _, _ = harness()
    request = registry.execute("visual_click", ARGS)
    def focus(handle):
        mouse.rectangle = WindowRectangle(0, 0, 800, 600)
        return True
    windows.set_foreground_window = focus
    assert not registry.confirm(request.confirmation_id).success
    assert not mouse.moves and mouse.clicks == 0


@pytest.mark.parametrize("utterance", ["Κοίτα το VS Code και πάτα το Search.", "Click the Search icon in VS Code.", "Koita to VS Code kai pata to Search."])
def test_provider_visual_click_routing(utterance):
    registry, _, _, _, _ = harness()
    client = FakeClient(SimpleNamespace(output=[function_call("visual_click", json.dumps(ARGS))]))
    provider = make_provider(client, registry.schemas())
    result = provider.resolve(utterance)
    assert result.action.tool_name == "visual_click" and result.action.arguments == ARGS
    assert utterance in _INSTRUCTIONS


def test_provider_excludes_and_rejects_visual_click_in_plans_and_second_decision():
    registry, _, _, _, _ = harness()
    schemas = registry.schemas()
    assert "visual_click" not in str(build_plan_tool_schema(schemas))
    client = FakeClient(SimpleNamespace(output=[function_call("visual_click", json.dumps(ARGS))]))
    provider = make_provider(client, schemas)
    assert "visual_click" not in str(provider._observation_tools)
    assert provider.decide_from_observation("Click Search", "observation").kind is IntentKind.UNSUPPORTED
    client.responses.response = SimpleNamespace(output=[function_call("propose_action_plan", json.dumps({"actions": [
        {"tool_name": "visual_click", "arguments": ARGS}, {"tool_name": "open_app", "arguments": {"app_name": "Chrome"}},
    ]}))])
    with pytest.raises(MalformedIntentResponseError, match="not allowed in an action plan"):
        provider.resolve("plan")


@pytest.mark.parametrize("observe", [True, False])
def test_assistant_defense_in_depth_rejects_visual_click_before_preparation(observe):
    registry, _, mouse, capture, _ = harness()
    class Intent:
        def resolve(self, request):
            if observe: return IntentResult.observe_ui_then_decide("VS Code")
            return IntentResult.action_plan((ToolAction("open_app", {"app_name": "Chrome"}), ToolAction("visual_click", ARGS)))
        def decide_from_observation(self, request, observation):
            return IntentResult.tool_action("visual_click", ARGS)
    assistant = Assistant(CommandRouter(), registry, Intent())
    assert not assistant.handle("test request").success
    assert not capture.calls and not mouse.rect_reads and not mouse.moves


def test_bootstrap_injects_mouse_and_confirm_does_not_call_intent_again():
    calls = []
    class Intent:
        def resolve(self, request):
            calls.append(request)
            return IntentResult.tool_action("visual_click", ARGS)
    windows = FakeWindowController((TARGET,), active_window=TARGET)
    mouse, capture, vision = FakeMouseClickController(), FakeWindowCaptureBackend(), FakeVisualPerceptionProvider()
    assistant = build_assistant(settings=Settings(openai_api_key=None), intent_provider=Intent(),
        window_controller=windows, mouse_click_controller=mouse, window_capture_backend=capture,
        visual_perception_provider=vision)
    response = assistant.handle("Click the Search icon in VS Code.")
    assert response.confirmation is not None
    assert assistant.confirm(response.confirmation.confirmation_id).success
    assert len(calls) == len(capture.calls) == len(vision.target_calls) == len(vision.refinement_calls) == 1
    assert mouse.clicks == 1
