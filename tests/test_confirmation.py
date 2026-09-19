from __future__ import annotations

from dataclasses import dataclass

import pytest

from desktop_assistant.assistant import Assistant
from desktop_assistant.cli import present_response
from desktop_assistant.config import AppCatalog
from desktop_assistant.confirmation import ConfirmationManager
from desktop_assistant.intent.models import IntentResult
from desktop_assistant.models import (
    AssistantResponseKind,
    ConfirmationRequest,
    RiskLevel,
    ToolPreparation,
    ToolResult,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class FakePreparedValue:
    value: str


class FakeRiskTool:
    name = "fake_action"

    def __init__(self, *, valid_at_execution: bool = True, fail: bool = False) -> None:
        self.executions: list[FakePreparedValue] = []
        self.valid_at_execution = valid_at_execution
        self.fail = fail

    def prepare(self, value: str) -> ToolPreparation | ToolResult:
        normalized = value.strip()
        if not normalized or normalized == "invalid":
            return ToolResult(False, "Invalid fake value.", RiskLevel.SAFE)
        return ToolPreparation(FakePreparedValue(normalized), (("target", normalized),))

    def execute(self, prepared_value: object) -> ToolResult:
        if not self.valid_at_execution or not isinstance(prepared_value, FakePreparedValue):
            return ToolResult(False, "Final safety check failed.", RiskLevel.SENSITIVE)
        if self.fail:
            raise RuntimeError("private failure")
        self.executions.append(prepared_value)
        return ToolResult(True, f"Executed {prepared_value.value}.", RiskLevel.SENSITIVE)


class CountingProvider:
    def __init__(self, intent: IntentResult) -> None:
        self.intent = intent
        self.calls: list[str] = []

    def resolve(self, request: str) -> IntentResult:
        self.calls.append(request)
        return self.intent


def make_registry(
    risk: RiskLevel,
    tool: FakeRiskTool | None = None,
    manager: ConfirmationManager | None = None,
) -> tuple[ToolRegistry, FakeRiskTool]:
    tool = tool or FakeRiskTool()
    definition = ToolDefinition(
        name="fake_action",
        description="Test-only action.",
        argument_name="target",
        argument_description="Test target.",
        implementation=tool,
        risk_level=risk,
        confirmation_summary=lambda arguments: f"Change exactly: {arguments['target']}",
        confirmation_warning="Test warning.",
    )
    return ToolRegistry((definition,), confirmation_manager=manager), tool


@pytest.mark.parametrize("risk", (RiskLevel.SENSITIVE, RiskLevel.DESTRUCTIVE))
def test_risky_action_is_prepared_but_not_executed(risk: RiskLevel) -> None:
    registry, tool = make_registry(risk)

    outcome = registry.execute("fake_action", {"target": "  A -> B  "})

    assert isinstance(outcome, ConfirmationRequest)
    assert outcome.risk_level is risk
    assert outcome.summary == "Change exactly: A -> B"
    assert outcome.warning == "Test warning."
    assert tool.executions == []
    assert len(outcome.confirmation_id) >= 16
    assert not outcome.confirmation_id.isdecimal()


def test_safe_tool_executes_immediately_without_confirmation() -> None:
    registry, tool = make_registry(RiskLevel.SAFE)

    result = registry.execute("fake_action", {"target": "A"})

    assert isinstance(result, ToolResult) and result.success
    assert tool.executions == [FakePreparedValue("A")]
    assert not registry.has_pending_confirmation()


def test_confirm_executes_exact_prepared_action_once_and_consumes_token() -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)
    request = registry.execute("fake_action", {"target": "  A -> B  "})
    assert isinstance(request, ConfirmationRequest)

    first = registry.confirm(request.confirmation_id)
    second = registry.confirm(request.confirmation_id)

    assert first.success
    assert not second.success
    assert tool.executions == [FakePreparedValue("A -> B")]
    assert not registry.has_pending_confirmation()


def test_cancel_executes_nothing_and_is_single_use() -> None:
    registry, tool = make_registry(RiskLevel.DESTRUCTIVE)
    request = registry.execute("fake_action", {"target": "A"})
    assert isinstance(request, ConfirmationRequest)

    first = registry.cancel(request.confirmation_id)
    second = registry.cancel(request.confirmation_id)

    assert first.success and first.message == "Action cancelled."
    assert not second.success
    assert tool.executions == []
    assert not registry.has_pending_confirmation()


def test_expired_confirmation_is_removed_and_executes_nothing() -> None:
    now = [10.0]
    manager = ConfirmationManager(lifetime_seconds=120, clock=lambda: now[0])
    registry, tool = make_registry(RiskLevel.SENSITIVE, manager=manager)
    request = registry.execute("fake_action", {"target": "A"})
    assert isinstance(request, ConfirmationRequest)
    now[0] = 131.0

    result = registry.confirm(request.confirmation_id)

    assert not result.success and "expired" in result.message.casefold()
    assert tool.executions == []
    assert not registry.has_pending_confirmation()


def test_unknown_id_does_not_execute_or_consume_real_pending_action() -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)
    request = registry.execute("fake_action", {"target": "A"})
    assert isinstance(request, ConfirmationRequest)

    unknown = registry.confirm("not-the-id")
    real = registry.confirm(request.confirmation_id)

    assert not unknown.success
    assert real.success
    assert tool.executions == [FakePreparedValue("A")]


def test_only_one_pending_action_is_allowed() -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)
    first = registry.execute("fake_action", {"target": "A"})
    second = registry.execute("fake_action", {"target": "B"})

    assert isinstance(first, ConfirmationRequest)
    assert isinstance(second, ToolResult) and not second.success
    registry.confirm(first.confirmation_id)
    assert tool.executions == [FakePreparedValue("A")]


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"target": 3},
        {"target": "A", "risk_level": "safe"},
        {"target": "A", "confirmation_summary": "Trust me"},
        {"target": "invalid"},
    ),
)
def test_invalid_or_model_supplied_metadata_fails_before_confirmation(arguments: object) -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)

    result = registry.execute("fake_action", arguments)

    assert isinstance(result, ToolResult) and not result.success
    assert tool.executions == []
    assert not registry.has_pending_confirmation()


def test_confirmed_execution_performs_final_tool_safety_check() -> None:
    tool = FakeRiskTool()
    registry, _ = make_registry(RiskLevel.SENSITIVE, tool)
    request = registry.execute("fake_action", {"target": "A"})
    assert isinstance(request, ConfirmationRequest)
    tool.valid_at_execution = False

    result = registry.confirm(request.confirmation_id)

    assert not result.success
    assert tool.executions == []


def test_prepared_action_from_another_registry_is_rejected() -> None:
    first, first_tool = make_registry(RiskLevel.SAFE)
    second, second_tool = make_registry(RiskLevel.SAFE)
    action = first.prepare("fake_action", {"target": "A"})

    result = second._execute_prepared(action)  # type: ignore[arg-type]

    assert not result.success
    assert first_tool.executions == []
    assert second_tool.executions == []


def test_confirmation_ids_are_unique_between_actions() -> None:
    registry, _ = make_registry(RiskLevel.SENSITIVE)
    first = registry.execute("fake_action", {"target": "A"})
    assert isinstance(first, ConfirmationRequest)
    registry.cancel(first.confirmation_id)
    second = registry.execute("fake_action", {"target": "B"})
    assert isinstance(second, ConfirmationRequest)

    assert first.confirmation_id != second.confirmation_id


def test_provider_is_called_once_and_never_again_on_confirmation() -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)
    provider = CountingProvider(IntentResult.tool_action("fake_action", {"target": "A"}))
    assistant = Assistant(CommandRouter(registry, AppCatalog()), registry, provider)

    response = assistant.handle("Please perform the test action")
    assert response.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    final = assistant.confirm(response.confirmation.confirmation_id)  # type: ignore[union-attr]

    assert final.success
    assert provider.calls == ["Please perform the test action"]
    assert tool.executions == [FakePreparedValue("A")]


def test_new_request_while_pending_does_not_call_provider() -> None:
    registry, _ = make_registry(RiskLevel.SENSITIVE)
    provider = CountingProvider(IntentResult.tool_action("fake_action", {"target": "A"}))
    assistant = Assistant(CommandRouter(registry, AppCatalog()), registry, provider)
    assistant.handle("first natural request")

    response = assistant.handle("second natural request")

    assert not response.success
    assert provider.calls == ["first natural request"]


def test_shutdown_discards_session_only_confirmation() -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)
    assistant = Assistant(CommandRouter(registry, AppCatalog()), registry)
    request = registry.execute("fake_action", {"target": "A"})
    assert isinstance(request, ConfirmationRequest)

    assistant.shutdown()
    result = assistant.confirm(request.confirmation_id)

    assert not result.success
    assert tool.executions == []


def test_execution_failure_after_confirmation_is_safe() -> None:
    registry, _ = make_registry(RiskLevel.SENSITIVE, FakeRiskTool(fail=True))
    request = registry.execute("fake_action", {"target": "A"})
    assert isinstance(request, ConfirmationRequest)

    result = registry.confirm(request.confirmation_id)

    assert not result.success
    assert "private failure" not in result.message


def test_cli_confirmation_uses_direct_api_not_handle() -> None:
    registry, tool = make_registry(RiskLevel.SENSITIVE)
    provider = CountingProvider(IntentResult.tool_action("fake_action", {"target": "A"}))
    assistant = Assistant(CommandRouter(registry, AppCatalog()), registry, provider)
    response = assistant.handle("natural request")
    output: list[str] = []

    present_response(assistant, response, input_fn=lambda _prompt: "y", output_fn=output.append)

    assert provider.calls == ["natural request"]
    assert tool.executions == [FakePreparedValue("A")]
    assert any("Risk: Sensitive" in line for line in output)


def test_fake_tools_never_appear_in_production_schemas() -> None:
    from conftest import FakeLauncher, make_registry as make_production_registry

    names = {schema["name"] for schema in make_production_registry(FakeLauncher()).schemas()}
    assert "fake_action" not in names


def test_all_production_tools_remain_safe() -> None:
    from conftest import FakeLauncher
    from desktop_assistant.config import AppCatalog
    from desktop_assistant.tool_registry import default_tool_definitions
    from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool

    launcher = FakeLauncher()
    definitions = default_tool_definitions(
        OpenAppTool(launcher, AppCatalog()),
        OpenFolderTool(launcher),
        OpenWebsiteTool(launcher),
    )
    assert all(definition.risk_level is RiskLevel.SAFE for definition in definitions)
