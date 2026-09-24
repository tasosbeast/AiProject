from __future__ import annotations

from pathlib import Path
import pytest

from conftest import (
    FakeLauncher,
    FakeUIActionController,
    FakeUIInspector,
    FakeWindowController,
    make_registry,
)
from desktop_assistant.assistant import MAX_OBSERVATION_CHARS, Assistant
from desktop_assistant.cancellation import CancellationToken
from desktop_assistant.intent.models import IntentResult, ToolAction
from desktop_assistant.intent.provider import IntentProviderUnavailableError
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.process_control import WindowInfo
from desktop_assistant.router import CommandRouter
from desktop_assistant.ui_perception import UIElementInfo, UIInspection


TARGET = WindowInfo(71, 17, "Untitled - Notepad", "notepad.exe", False)


def make_element_info(
    control_type: str,
    name: str,
    capabilities: tuple[str, ...] = (),
    automation_id: str = "",
) -> UIElementInfo:
    return UIElementInfo(
        control_type=control_type,
        name=name,
        automation_id=automation_id,
        enabled=True,
        focusable=True,
        focused=False,
        offscreen=False,
        capabilities=capabilities,
    )


class FakeObserveProvider:
    def __init__(
        self,
        first_result: IntentResult,
        second_result: IntentResult | None = None,
        second_error: Exception | None = None,
    ) -> None:
        self.first_result = first_result
        self.second_result = second_result
        self.second_error = second_error
        self.resolve_calls: list[str] = []
        self.decide_calls: list[tuple[str, str]] = []

    def resolve(self, request: str) -> IntentResult:
        self.resolve_calls.append(request)
        return self.first_result

    def decide_from_observation(self, request: str, observation: str) -> IntentResult:
        self.decide_calls.append((request, observation))
        if self.second_error is not None:
            raise self.second_error
        if self.second_result is None:
            raise AssertionError("FakeObserveProvider had no second_result configured.")
        return self.second_result


def make_harness(
    provider: object,
    *,
    windows: tuple[WindowInfo, ...] = (TARGET,),
    inspection: UIInspection | None = None,
    action_controller: FakeUIActionController | None = None,
    inspector: FakeUIInspector | None = None,
) -> tuple[Assistant, FakeUIInspector, FakeUIActionController]:
    fake_inspector = inspector or FakeUIInspector(
        inspection=inspection
        or UIInspection(
            controls=(
                make_element_info("button", "Settings", ("invoke",), "auto_settings"),
                make_element_info("check_box", "Spell check", ("toggle",), "auto_spellcheck"),
            ),
            truncated=False,
        )
    )
    fake_action_controller = action_controller or FakeUIActionController()
    win_ctrl = FakeWindowController(windows)
    registry = make_registry(
        FakeLauncher(),
        window_controller=win_ctrl,
        ui_inspector=fake_inspector,
        ui_action_controller=fake_action_controller,
    )
    assistant = Assistant(
        router=CommandRouter(),
        tool_registry=registry,
        intent_provider=provider,
    )
    return assistant, fake_inspector, fake_action_controller


def test_vague_ui_goal_triggers_observe_decide_confirm_flow() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.tool_action(
        "ui_action",
        {"query": "Notepad", "control": "Settings", "action": "invoke"},
    )
    provider = FakeObserveProvider(first, second)
    assistant, inspector, action_ctrl = make_harness(provider)

    req = "Άνοιξε τις ρυθμίσεις του Notepad."
    response = assistant.handle(req)

    # 1. First provider call decided observation was needed
    assert provider.resolve_calls == [req]
    # 2. SAFE ui_inspect executed once
    assert inspector.calls == [(TARGET.handle, TARGET.process_id)]
    # 3. Second provider call received original request + observation
    assert len(provider.decide_calls) == 1
    call_req, call_obs = provider.decide_calls[0]
    assert call_req == req
    assert "button — Settings" in call_obs
    assert "check_box — Spell check" in call_obs
    assert "invoke" in call_obs
    assert "toggle" in call_obs
    # Verify internal HWND, PID, and RuntimeId are not exposed
    assert str(TARGET.handle) not in call_obs
    assert str(TARGET.process_id) not in call_obs
    assert "runtime_id" not in call_obs

    # Max provider calls total is 2
    assert len(provider.resolve_calls) + len(provider.decide_calls) == 2

    # Sensitive action requires confirmation
    assert response.confirmation is not None
    assert "Settings" in response.confirmation.summary

    # Execute confirmation
    confirm_response = assistant.confirm(response.confirmation.confirmation_id)
    assert confirm_response.success

    # Confirmation must NOT trigger third provider call or second inspection
    assert len(provider.resolve_calls) == 1
    assert len(provider.decide_calls) == 1
    assert len(inspector.calls) == 1
    assert len(action_ctrl.execute_calls) == 1


def test_observe_decide_cancel_zero_mutation() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.tool_action(
        "ui_action",
        {"query": "Notepad", "control": "Settings", "action": "invoke"},
    )
    provider = FakeObserveProvider(first, second)
    assistant, _, action_ctrl = make_harness(provider)

    response = assistant.handle("Open Notepad settings.")
    assert response.confirmation is not None

    cancel_response = assistant.cancel(response.confirmation.confirmation_id)
    assert cancel_response.success

    # Zero final mutation
    assert len(action_ctrl.execute_calls) == 0
    # No extra provider calls
    assert len(provider.resolve_calls) == 1
    assert len(provider.decide_calls) == 1


def test_explicit_ui_action_bypasses_observation() -> None:
    direct = IntentResult.tool_action(
        "ui_action",
        {"query": "Notepad", "control": "Settings", "action": "invoke"},
    )
    provider = FakeObserveProvider(direct)
    assistant, inspector, _ = make_harness(provider)

    response = assistant.handle("Πάτα Settings στο Notepad.")
    assert response.confirmation is not None

    # Direct action: zero observation calls, zero second provider calls
    assert len(inspector.calls) == 0
    assert len(provider.decide_calls) == 0
    assert len(provider.resolve_calls) == 1


def test_observation_failure_stops_safely_without_second_provider_call() -> None:
    first = IntentResult.observe_ui_then_decide("Calculator")
    second = IntentResult.tool_action(
        "ui_action",
        {"query": "Calculator", "control": "Five", "action": "invoke"},
    )
    provider = FakeObserveProvider(first, second)
    # Calculator window does not exist (only Notepad exists)
    assistant, inspector, _ = make_harness(provider)

    response = assistant.handle("Open Calculator settings.")
    assert not response.success
    assert "No matching window found for 'Calculator'." in response.message

    # ui_inspect attempted, but second provider call NEVER occurs
    assert len(inspector.calls) == 0
    assert len(provider.decide_calls) == 0


def test_second_provider_attempts_observation_rejected() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    # Second provider tries to observe again
    second = IntentResult.observe_ui_then_decide("Notepad")
    provider = FakeObserveProvider(first, second)
    assistant, _, action_ctrl = make_harness(provider)

    response = assistant.handle("Open settings.")
    assert not response.success
    assert "Observation cannot be chained or planned." in response.message
    assert len(action_ctrl.execute_calls) == 0


def test_second_provider_attempts_action_plan_rejected() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    # Second provider tries to return a plan
    second = IntentResult.action_plan(
        (
            ToolAction("ui_action", {"query": "Notepad", "control": "Settings", "action": "invoke"}),
            ToolAction("ui_action", {"query": "Notepad", "control": "File", "action": "expand"}),
        )
    )
    provider = FakeObserveProvider(first, second)
    assistant, _, action_ctrl = make_harness(provider)

    response = assistant.handle("Open settings and expand file.")
    assert not response.success
    assert "Observation cannot be chained or planned." in response.message
    assert len(action_ctrl.execute_calls) == 0


def test_second_provider_returns_conversational_response() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.conversation("The settings button is located at the top right.")
    provider = FakeObserveProvider(first, second)
    assistant, _, _ = make_harness(provider)

    response = assistant.handle("Where are the settings?")
    assert response.success
    assert response.message == "The settings button is located at the top right."


def test_second_provider_returns_unsupported_response() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.unsupported("Could not find any settings toggle.")
    provider = FakeObserveProvider(first, second)
    assistant, _, _ = make_harness(provider)

    response = assistant.handle("Turn off dark mode.")
    assert not response.success
    assert response.message == "Could not find any settings toggle."


def test_second_provider_returns_safe_tool_action() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.tool_action("window_info", {"action": "list"})
    provider = FakeObserveProvider(first, second)
    assistant, _, _ = make_harness(provider)

    response = assistant.handle("Check windows.")
    # Safe action executes directly without confirmation
    assert response.confirmation is None
    assert response.success


def test_second_provider_failure_returns_safe_error() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    provider = FakeObserveProvider(first, second_error=IntentProviderUnavailableError("Timeout"))
    assistant, _, _ = make_harness(provider)

    response = assistant.handle("Open settings.")
    assert not response.success
    assert "AI routing is temporarily unavailable." in response.message


def test_cancellation_during_observe_decide_prevents_action() -> None:
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.tool_action(
        "ui_action",
        {"query": "Notepad", "control": "Settings", "action": "invoke"},
    )
    provider = FakeObserveProvider(first, second)
    assistant, _, action_ctrl = make_harness(provider)

    token = CancellationToken()
    token.cancel()

    response = assistant.handle("Open settings.", cancellation_token=token)
    assert not response.success
    assert "Request was cancelled." in response.message
    assert len(action_ctrl.execute_calls) == 0


def test_observation_length_is_bounded() -> None:
    # Generate 100 controls to exceed MAX_OBSERVATION_CHARS
    many_controls = tuple(
        make_element_info("button", f"Button {i} with a very long descriptive name for testing bounding", ("invoke",), f"auto_{i}")
        for i in range(100)
    )
    first = IntentResult.observe_ui_then_decide("Notepad")
    second = IntentResult.conversation("Observed.")
    provider = FakeObserveProvider(first, second)
    assistant, _, _ = make_harness(
        provider,
        inspection=UIInspection(controls=many_controls, truncated=False),
    )

    assistant.handle("Inspect Notepad.")
    assert len(provider.decide_calls) == 1
    _, observation = provider.decide_calls[0]
    assert len(observation) <= MAX_OBSERVATION_CHARS
    assert "[Observation truncated]" in observation
