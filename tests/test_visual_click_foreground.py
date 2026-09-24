from dataclasses import replace

import pytest

from conftest import (FakeForegroundClock, FakeMouseClickController, FakeVisualPerceptionProvider,
                      FakeWindowCaptureBackend, FakeWindowController)
from test_visual_perception import TARGET
from desktop_assistant.config import AppCatalog
from desktop_assistant.models import ConfirmationRequest
from desktop_assistant.tool_registry import ToolRegistry, _additional_tool_definition
from desktop_assistant.visual_click import VisualClickTool
from desktop_assistant.visual_perception import VisualTargetTool


@pytest.mark.parametrize("returned,delay", [(True, 0), (False, 0), (False, 2), (True, 3)])
def test_exact_foreground_observation_allows_click_without_retargeting(returned, delay):
    check_wait(returned, delay, TARGET, True)


@pytest.mark.parametrize("wrong,reason", [
    (None, "no foreground window observed"),
    (replace(TARGET, handle=88776655), "HWND mismatch"),
    (replace(TARGET, process_id=99887766), "PID mismatch"),
    (replace(TARGET, title="private title"), "title mismatch"),
    (replace(TARGET, executable_name="private.exe"), "executable mismatch"),
    (replace(TARGET, minimized=True), "target observed minimized"),
])
def test_never_exact_foreground_times_out_without_mouse_mutation(wrong, reason, caplog):
    result = check_wait(True, 0, wrong, False, diagnostic=reason)
    for private in ("88776655", "99887766", "private title", "private.exe", "base64", "normalized_bounds"):
        assert private not in str(result) and private not in caplog.text


def check_wait(returned, delay, observed, success, *, frozen_clock=False,
               diagnostic="no foreground window observed", packaged=False):
    timer = FakeForegroundClock()
    windows = FakeWindowController((TARGET,))
    mouse, capture, provider = FakeMouseClickController(), FakeWindowCaptureBackend(), FakeVisualPerceptionProvider()
    focus_calls, polls = [], []
    def foreground():
        polls.append(None)
        if callable(observed):
            return observed(len(polls))
        return observed if len(timer.sleeps) >= delay else None
    def focus(handle):
        focus_calls.append(handle)
        return returned
    def sleeper(seconds):
        # Preparation is finished; every wait is free of mutation and AI work.
        assert not mouse.moves and mouse.clicks == 0
        assert len(capture.calls) == len(provider.target_calls) == len(provider.refinement_calls) == 1
        timer.sleep(seconds)
    windows.get_foreground_window = foreground
    windows.set_foreground_window = focus
    tool = VisualClickTool(windows, VisualTargetTool(windows, AppCatalog(), capture, provider),
                           mouse, clock=(lambda: 0.0) if frozen_clock else timer.clock, sleeper=sleeper)
    registry = ToolRegistry((_additional_tool_definition(tool),))
    request = registry.execute("visual_click", {"query": "VS Code", "target": "Search"})
    assert isinstance(request, ConfirmationRequest)
    assert not focus_calls and not timer.sleeps
    result = registry.confirm(request.confirmation_id)
    assert result.success is success
    assert focus_calls == [TARGET.handle]
    assert len(capture.calls) == len(provider.target_calls) == len(provider.refinement_calls) == 1
    assert sum(timer.sleeps) <= 0.3 + 1e-9
    assert len(timer.sleeps) <= 10
    if success:
        assert len(timer.sleeps) == delay
        assert mouse.clicks == 1 and len(mouse.moves) == 1
        assert "Diagnostic:" not in result.message
    else:
        expected = "Windows could not verify foreground focus. No click sent."
        if not packaged:
            expected += f"\nDiagnostic: {diagnostic}."
        assert result.message == expected
        assert len(polls) <= 11
        assert not mouse.moves and mouse.clicks == 0
        assert len(timer.sleeps) == 10
    return result


def test_fixed_poll_cap_even_if_clock_does_not_advance():
    check_wait(False, 0, None, False, frozen_clock=True)


def test_packaged_failure_remains_generic(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    check_wait(True, 0, replace(TARGET, title="private"), False, packaged=True)


def test_latest_concrete_mismatch_survives_final_empty_observations():
    def observed(poll):
        if poll == 1: return replace(TARGET, handle=88776655)
        if poll == 2: return replace(TARGET, title="changed")
        if poll == 3: return replace(TARGET, executable_name="other.exe")
        return None
    check_wait(True, 0, observed, False, diagnostic="executable mismatch")


def test_case_insensitive_executable_is_still_exact_match():
    check_wait(True, 0, replace(TARGET, executable_name=TARGET.executable_name.upper()), True)
