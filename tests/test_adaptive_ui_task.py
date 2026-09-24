from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest

from conftest import (
    FakeEditableControlResolver,
    FakeInputController,
    FakeLauncher,
    FakeMouseClickController,
    FakeUIActionController,
    FakeUIInspector,
    FakeVisualPerceptionProvider,
    FakeWindowCaptureBackend,
    FakeWindowController,
    make_registry,
)
from desktop_assistant.assistant import MAX_OBSERVATION_CHARS, Assistant
from desktop_assistant.cancellation import CancellationToken
from desktop_assistant.confirmation import PreparedAction
from desktop_assistant.intent.models import ActionPlan, IntentKind, IntentResult, ToolAction
from desktop_assistant.intent.provider import IntentProviderUnavailableError
from desktop_assistant.models import (
    AssistantResponseKind,
    ConfirmationRequest,
    PlanContext,
    RiskLevel,
    ToolResult,
)
from desktop_assistant.process_control import WindowInfo
from desktop_assistant.router import CommandRouter
from desktop_assistant.ui_perception import UIElementInfo, UIInspection


TARGET = WindowInfo(101, 202, "Visual Studio Code", "code.exe", False)


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


class FakeAdaptiveProvider:
    def __init__(
        self,
        resolve_result: IntentResult,
        step_decisions: list[IntentResult | Exception] | None = None,
    ) -> None:
        self.resolve_result = resolve_result
        self.step_decisions = list(step_decisions or [])
        self.resolve_calls: list[str] = []
        self.decide_calls: list[dict[str, Any]] = []

    def resolve(self, request: str) -> IntentResult:
        self.resolve_calls.append(request)
        return self.resolve_result

    def decide_from_observation(self, request: str, observation: str) -> IntentResult:
        raise NotImplementedError("Not used in adaptive tasks")

    def decide_adaptive_ui_step(
        self,
        original_request: str,
        target_query: str,
        step_number: int,
        bounded_observation: str,
        bounded_history: str,
    ) -> IntentResult:
        self.decide_calls.append(
            {
                "original_request": original_request,
                "target_query": target_query,
                "step_number": step_number,
                "observation": bounded_observation,
                "history": bounded_history,
            }
        )
        if not self.step_decisions:
            raise AssertionError(f"No configured decision for step {step_number}")
        next_decision = self.step_decisions.pop(0)
        if isinstance(next_decision, Exception):
            raise next_decision
        return next_decision


def make_adaptive_harness(
    provider: object,
    *,
    windows: tuple[WindowInfo, ...] = (TARGET,),
    inspection: UIInspection | None = None,
    inspector: FakeUIInspector | None = None,
    action_controller: FakeUIActionController | None = None,
    click_controller: FakeMouseClickController | None = None,
    input_controller: FakeInputController | None = None,
) -> tuple[Assistant, FakeUIInspector, Any]:
    fake_inspector = inspector or FakeUIInspector(
        inspection=inspection
        or UIInspection(
            controls=(
                make_element_info("button", "Search", ("invoke",), "auto_search"),
                make_element_info("edit", "Search input", ("invoke",), "auto_input"),
            ),
            truncated=False,
        )
    )
    fake_action_controller = action_controller or FakeUIActionController()
    fake_click_controller = click_controller or FakeMouseClickController()
    fake_input_controller = input_controller or FakeInputController()
    win_ctrl = FakeWindowController(windows, active_window=windows[0] if windows else None)
    registry = make_registry(
        FakeLauncher(),
        window_controller=win_ctrl,
        ui_inspector=fake_inspector,
        ui_action_controller=fake_action_controller,
        mouse_click_controller=fake_click_controller,
        input_controller=fake_input_controller,
        editable_control_resolver=FakeEditableControlResolver(),
    )
    assistant = Assistant(
        router=CommandRouter(),
        tool_registry=registry,
        intent_provider=provider,
    )
    return assistant, fake_inspector, registry


def test_adaptive_ui_task_full_2step_happy_path() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task(
            goal="Click Search and type Bookish",
            query="Visual Studio Code",
        ),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, inspector, registry = make_adaptive_harness(provider)

    # 1. User issues adaptive command -> starts step 1
    resp1 = assistant.handle("Πάτα Search στο VS Code και μετά γράψε Bookish.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert resp1.confirmation is not None
    conf1 = resp1.confirmation
    assert conf1.risk_level is RiskLevel.SENSITIVE
    assert conf1.plan_context is not None
    assert conf1.plan_context.step_index == 1
    assert conf1.plan_context.total_steps == 2
    assert assistant.has_pending_confirmation() is True

    # Check that another command is blocked while confirmation is pending
    busy_resp = assistant.handle("Άνοιξε το Chrome.")
    assert busy_resp.kind is AssistantResponseKind.COMPLETED
    assert "Confirm or cancel the pending action before starting another request." in busy_resp.message

    # Verify provider call 1
    assert len(provider.decide_calls) == 1
    assert provider.decide_calls[0]["step_number"] == 1
    assert provider.decide_calls[0]["history"] == "None"

    # 2. Confirm step 1 -> step 1 executes, re-observes, and returns confirmation for step 2
    resp2 = assistant.confirm(conf1.confirmation_id)
    assert resp2.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert resp2.confirmation is not None
    conf2 = resp2.confirmation
    assert conf2.confirmation_id != conf1.confirmation_id  # Step 1 confirmation did NOT authorize step 2!
    assert conf2.plan_context is not None
    assert conf2.plan_context.step_index == 2
    assert conf2.plan_context.total_steps == 2
    assert len(conf2.plan_context.completed_summaries) == 1
    assert assistant.has_pending_confirmation() is True

    # Verify provider call 2
    assert len(provider.decide_calls) == 2
    assert provider.decide_calls[1]["step_number"] == 2
    assert "Step 1:" in provider.decide_calls[1]["history"]

    # 3. Confirm step 2 -> step 2 executes, optional final verification inspects, task completes!
    resp3 = assistant.confirm(conf2.confirmation_id)
    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result is not None
    assert resp3.result.success is True
    assert "Completed adaptive UI task with 2 actions:" in resp3.result.message
    assert "1." in resp3.result.message
    assert "2." in resp3.result.message
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_cancel_step_1_zero_mutations() -> None:
    action_ctrl = FakeUIActionController()
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider, action_controller=action_ctrl)

    resp1 = assistant.handle("Search in VS Code.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    conf_id = resp1.confirmation.confirmation_id

    cancel_resp = assistant.cancel(conf_id)
    assert cancel_resp.kind is AssistantResponseKind.COMPLETED
    assert cancel_resp.result.success is True
    assert "Adaptive task cancelled at step 1 of 2. Remaining actions were discarded." in cancel_resp.result.message
    assert assistant.has_pending_confirmation() is False
    assert len(action_ctrl.execute_calls) == 0


def test_adaptive_ui_task_cancel_step_2_leaves_step_1_completed() -> None:
    action_ctrl = FakeUIActionController()
    input_ctrl = FakeInputController()
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider, action_controller=action_ctrl, input_controller=input_ctrl)

    resp1 = assistant.handle("Search and type.")
    conf1_id = resp1.confirmation.confirmation_id

    resp2 = assistant.confirm(conf1_id)
    assert resp2.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    conf2_id = resp2.confirmation.confirmation_id

    # Step 1 was executed
    assert len(action_ctrl.execute_calls) == 1
    # Step 2 has not executed yet
    assert len(input_ctrl.calls) == 0

    cancel_resp = assistant.cancel(conf2_id)
    assert cancel_resp.kind is AssistantResponseKind.COMPLETED
    assert cancel_resp.result.success is True
    assert "Adaptive task cancelled at step 2 of 2. 1 action(s) completed before cancellation." in cancel_resp.result.message
    assert assistant.has_pending_confirmation() is False
    # Step 2 never executed
    assert len(input_ctrl.calls) == 0


def test_adaptive_ui_task_step_1_execution_failure_stops_without_step_2_observation() -> None:
    action_ctrl = FakeUIActionController()
    action_ctrl.execute_result = ToolResult(False, "Failed to invoke Search control.", RiskLevel.SENSITIVE)
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, inspector, _ = make_adaptive_harness(provider, action_controller=action_ctrl)

    resp1 = assistant.handle("Search and type.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    conf1_id = resp1.confirmation.confirmation_id

    initial_inspect_count = len(inspector.calls)

    resp2 = assistant.confirm(conf1_id)
    assert resp2.kind is AssistantResponseKind.COMPLETED
    assert resp2.result.success is False
    assert "Adaptive task stopped at step 1 of 2: Failed to invoke Search control." in resp2.result.message
    assert "Remaining steps were not run." in resp2.result.message
    assert assistant.has_pending_confirmation() is False

    # Step 2 observation was never run
    assert len(inspector.calls) == initial_inspect_count
    # Second decision was never called
    assert len(provider.decide_calls) == 1


def test_adaptive_ui_task_step_1_observation_failure_fails_closed() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[],
    )
    # Inspector with no matching window
    assistant, _, _ = make_adaptive_harness(provider, windows=())

    resp = assistant.handle("Search in VS Code.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is False
    assert assistant.has_pending_confirmation() is False
    assert len(provider.decide_calls) == 0


def test_adaptive_ui_task_step_2_observation_failure_reports_step_1_completed() -> None:
    class FailingSecondInspector:
        def __init__(self) -> None:
            self.count = 0

        def inspect(self, handle: int, process_id: int) -> UIInspection:
            self.count += 1
            if self.count == 1:
                return UIInspection(
                    controls=(make_element_info("button", "Search", ("invoke",)),),
                    truncated=False,
                )
            raise RuntimeError("UIA disconnected during step 2")

    failing_inspector = FailingSecondInspector()
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider, inspector=failing_inspector)

    resp1 = assistant.handle("Search and type.")
    conf1_id = resp1.confirmation.confirmation_id

    resp2 = assistant.confirm(conf1_id)
    assert resp2.kind is AssistantResponseKind.COMPLETED
    assert resp2.result.success is False
    assert "Adaptive task stopped at step 2 observation:" in resp2.result.message
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_provider_unavailable_fails_safely() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[IntentProviderUnavailableError("Timeout")],
    )
    assistant, _, _ = make_adaptive_harness(provider)

    resp = assistant.handle("Search in VS Code.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is False
    assert resp.result.message == "AI routing is temporarily unavailable."
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_complete_at_step_1() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[IntentResult.complete("Nothing to do.")],
    )
    assistant, _, _ = make_adaptive_harness(provider)

    resp = assistant.handle("Search in VS Code.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is True
    assert resp.result.message == "Nothing to do."
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_complete_at_step_2() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.complete("Search already opened with target text."),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider)

    resp1 = assistant.handle("Search in VS Code.")
    resp2 = assistant.confirm(resp1.confirmation.confirmation_id)
    assert resp2.kind is AssistantResponseKind.COMPLETED
    assert resp2.result.success is True
    assert "Completed adaptive UI task with 1 actions:" in resp2.result.message
    assert "Search already opened with target text." in resp2.result.message
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_unsupported_at_step_1() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[IntentResult.unsupported("Search button not visible.")],
    )
    assistant, _, _ = make_adaptive_harness(provider)

    resp = assistant.handle("Search in VS Code.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is False
    assert resp.result.message == "Search button not visible."
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_rejects_forbidden_tools() -> None:
    for forbidden_tool in ("open_app", "propose_action_plan", "observe_ui_then_decide", "shell_exec"):
        provider = FakeAdaptiveProvider(
            resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
            step_decisions=[IntentResult.tool_action(forbidden_tool, {"query": "Visual Studio Code"})],
        )
        assistant, _, _ = make_adaptive_harness(provider)

        resp = assistant.handle("Search in VS Code.")
        assert resp.kind is AssistantResponseKind.COMPLETED
        assert resp.result.success is False
        assert f"Tool '{forbidden_tool}' is not allowed in an adaptive UI task." in resp.result.message
        assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_hard_bound_max_2_mutations() -> None:
    # Even if provider wanted 3 mutations, the state machine finishes at step 2
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "enter"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider)

    resp1 = assistant.handle("Search in VS Code.")
    resp2 = assistant.confirm(resp1.confirmation.confirmation_id)
    resp3 = assistant.confirm(resp2.confirmation.confirmation_id)

    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result.success is True
    assert "Completed adaptive UI task with 2 actions:" in resp3.result.message
    # Third decision is never called
    assert len(provider.decide_calls) == 2
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_shutdown_discards_task() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider)

    resp1 = assistant.handle("Search in VS Code.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert assistant.has_pending_confirmation() is True

    assistant.shutdown()
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_with_visual_click_and_window_input() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Click Search icon and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("visual_click", {"query": "Visual Studio Code", "target": "Search"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    click_ctrl = FakeMouseClickController()
    input_ctrl = FakeInputController()
    assistant, _, _ = make_adaptive_harness(provider, click_controller=click_ctrl, input_controller=input_ctrl)

    resp1 = assistant.handle("Click Search icon and type Bookish in VS Code.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert resp1.confirmation.risk_level is RiskLevel.SENSITIVE

    resp2 = assistant.confirm(resp1.confirmation.confirmation_id)
    assert resp2.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert resp2.confirmation.risk_level is RiskLevel.SENSITIVE
    assert click_ctrl.clicks == 1

    resp3 = assistant.confirm(resp2.confirmation.confirmation_id)
    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result.success is True
    assert len(input_ctrl.calls) == 1
    assert "Completed adaptive UI task with 2 actions:" in resp3.result.message


def test_adaptive_ui_task_history_sanitization_and_bounding() -> None:
    long_desc = "x" * 3000
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.complete("Done"),
        ],
    )
    action_ctrl = FakeUIActionController()
    action_ctrl.execute_result = ToolResult(True, long_desc, RiskLevel.SENSITIVE)
    assistant, _, _ = make_adaptive_harness(provider, action_controller=action_ctrl)

    resp1 = assistant.handle("Search in VS Code.")
    resp2 = assistant.confirm(resp1.confirmation.confirmation_id)
    assert resp2.kind is AssistantResponseKind.COMPLETED

    assert len(provider.decide_calls) == 2
    step2_history = provider.decide_calls[1]["history"]
    assert len(step2_history) <= MAX_OBSERVATION_CHARS
    assert "[History truncated]" in step2_history
    # Ensure no internal IDs leaked
    assert "hwnd" not in step2_history.lower()
    assert "pid" not in step2_history.lower()
    assert "runtimeid" not in step2_history.lower()


def test_existing_action_plan_and_observe_ui_then_decide_unaffected() -> None:
    # 1. Action plan still works
    plan_provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.action_plan(
            [
                ToolAction("focus_window", {"query": "Visual Studio Code"}),
                ToolAction("focus_window", {"query": "Visual Studio Code"}),
            ]
        )
    )
    assistant, _, _ = make_adaptive_harness(plan_provider)
    resp = assistant.handle("Perform two actions.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is True
    assert "Completed 2 actions:" in resp.result.message


def test_adaptive_ui_task_rejects_destructive_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
        ],
    )
    assistant, _, registry = make_adaptive_harness(provider)

    original_prepare = registry.prepare

    def fake_prepare(tool_name: str, arguments: dict[str, Any]) -> PreparedAction | ToolResult:
        prepared = original_prepare(tool_name, arguments)
        if isinstance(prepared, PreparedAction) and tool_name == "ui_action":
            return replace(prepared, risk_level=RiskLevel.DESTRUCTIVE)
        return prepared

    monkeypatch.setattr(registry, "prepare", fake_prepare)

    resp = assistant.handle("Search in VS Code.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is False
    assert "Adaptive tasks cannot contain destructive actions." in resp.result.message
    assert assistant.has_pending_confirmation() is False


def test_adaptive_ui_task_step_1_query_mismatch_rejected() -> None:
    action_ctrl = FakeUIActionController()
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Google Chrome", "control": "Search", "action": "invoke"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider, action_controller=action_ctrl)

    resp = assistant.handle("Search in VS Code.")
    assert resp.kind is AssistantResponseKind.COMPLETED
    assert resp.result.success is False
    assert resp.result.message == "Adaptive task step targeted a different window and was rejected."
    assert assistant.has_pending_confirmation() is False
    assert len(action_ctrl.execute_calls) == 0
    assert len(provider.decide_calls) == 1
    assert assistant._pending_adaptive_task is None


def test_adaptive_ui_task_step_2_query_mismatch_rejected() -> None:
    action_ctrl = FakeUIActionController()
    input_ctrl = FakeInputController()
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Google Chrome", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, inspector, _ = make_adaptive_harness(provider, action_controller=action_ctrl, input_controller=input_ctrl)

    resp1 = assistant.handle("Search in VS Code then type.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    conf1 = resp1.confirmation
    assert conf1 is not None

    resp2 = assistant.confirm(conf1.confirmation_id)
    assert resp2.kind is AssistantResponseKind.COMPLETED
    assert resp2.result.success is False
    assert resp2.result.message == "Adaptive task step targeted a different window and was rejected."
    assert assistant.has_pending_confirmation() is False
    assert len(action_ctrl.execute_calls) == 1
    assert len(input_ctrl.calls) == 0
    assert len(provider.decide_calls) == 2
    assert len(inspector.calls) == 2
    assert assistant._pending_adaptive_task is None


def test_adaptive_ui_task_query_normalized_match_accepted() -> None:
    action_ctrl = FakeUIActionController()
    input_ctrl = FakeInputController()
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Search and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "  visual studio code  ", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "VISUAL STUDIO CODE", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, _, _ = make_adaptive_harness(provider, action_controller=action_ctrl, input_controller=input_ctrl)

    resp1 = assistant.handle("Search in VS Code.")
    assert resp1.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    conf1 = resp1.confirmation
    assert conf1 is not None

    resp2 = assistant.confirm(conf1.confirmation_id)
    assert resp2.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    conf2 = resp2.confirmation
    assert conf2 is not None

    resp3 = assistant.confirm(conf2.confirmation_id)
    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result.success is True
    assert len(action_ctrl.execute_calls) == 1
    assert len(input_ctrl.calls) == 1
    assert assistant._pending_adaptive_task is None


def test_adaptive_ui_task_final_verification_success() -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Click and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, inspector, _ = make_adaptive_harness(provider)

    resp1 = assistant.handle("Click Search and type.")
    conf1 = resp1.confirmation
    assert conf1 is not None
    resp2 = assistant.confirm(conf1.confirmation_id)
    conf2 = resp2.confirmation
    assert conf2 is not None
    resp3 = assistant.confirm(conf2.confirmation_id)

    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result.success is True
    assert "Completed adaptive UI task with 2 actions:" in resp3.result.message
    assert "Final UI verification completed." in resp3.result.message
    assert len(inspector.calls) == 3
    assert len(provider.decide_calls) == 2


def test_adaptive_ui_task_final_verification_failure_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Click and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, inspector, registry = make_adaptive_harness(provider)

    original_execute = registry.execute

    def fake_execute(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if tool_name == "ui_inspect" and len(inspector.calls) >= 2:
            return ToolResult(False, "Window lost or inspection failed.", RiskLevel.SAFE)
        return original_execute(tool_name, arguments)

    monkeypatch.setattr(registry, "execute", fake_execute)

    resp1 = assistant.handle("Click Search and type.")
    conf1 = resp1.confirmation
    assert conf1 is not None
    resp2 = assistant.confirm(conf1.confirmation_id)
    conf2 = resp2.confirmation
    assert conf2 is not None
    resp3 = assistant.confirm(conf2.confirmation_id)

    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result.success is True
    assert "Completed adaptive UI task with 2 actions:" in resp3.result.message
    assert "Final UI verification was unavailable." in resp3.result.message
    assert len(provider.decide_calls) == 2


def test_adaptive_ui_task_final_verification_exception_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeAdaptiveProvider(
        resolve_result=IntentResult.adaptive_ui_task("Click and type", "Visual Studio Code"),
        step_decisions=[
            IntentResult.tool_action("ui_action", {"query": "Visual Studio Code", "control": "Search", "action": "invoke"}),
            IntentResult.tool_action("window_input", {"query": "Visual Studio Code", "action": "type_text", "value": "Bookish"}),
        ],
    )
    assistant, inspector, registry = make_adaptive_harness(provider)

    original_execute = registry.execute

    def fake_execute(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if tool_name == "ui_inspect" and len(inspector.calls) >= 2:
            raise RuntimeError("Unexpected inspect failure")
        return original_execute(tool_name, arguments)

    monkeypatch.setattr(registry, "execute", fake_execute)

    resp1 = assistant.handle("Click Search and type.")
    conf1 = resp1.confirmation
    assert conf1 is not None
    resp2 = assistant.confirm(conf1.confirmation_id)
    conf2 = resp2.confirmation
    assert conf2 is not None
    resp3 = assistant.confirm(conf2.confirmation_id)

    assert resp3.kind is AssistantResponseKind.COMPLETED
    assert resp3.result.success is True
    assert "Completed adaptive UI task with 2 actions:" in resp3.result.message
    assert "Final UI verification was unavailable." in resp3.result.message
    assert len(provider.decide_calls) == 2




