from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.assistant import Assistant
from desktop_assistant.cancellation import CancellationToken
from desktop_assistant.cli import present_response
from desktop_assistant.config import AppCatalog
from desktop_assistant.filesystem import FilesystemPathValidator
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    RenamePathTool,
)
from desktop_assistant.intent.models import ActionPlan, IntentKind, IntentResult, ToolAction
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.models import (
    AssistantResponseKind,
    ConfirmationRequest,
    PlanContext,
    RiskLevel,
    ToolArguments,
    ToolPreparation,
    ToolResult,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.safety import SafetyPolicy
from desktop_assistant.tool_registry import ToolDefinition, ToolRegistry, default_tool_definitions, string_argument
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool
from conftest import FakeLauncher, FakeProcessController


class FakeProvider:
    def __init__(self, result: IntentResult | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    def resolve(self, request: str) -> IntentResult:
        self.calls.append(request)
        assert self.result is not None
        return self.result


def make_test_assistant(
    launcher: FakeLauncher,
    provider: FakeProvider | None,
    *,
    process_controller: FakeProcessController | None = None,
    home: Path | None = None,
    extra_tools: tuple[ToolDefinition, ...] = (),
) -> Assistant:
    catalog = AppCatalog()
    validator = FilesystemPathValidator()
    process_controller = process_controller or FakeProcessController()
    tools = list(
        default_tool_definitions(
            OpenAppTool(launcher, catalog),
            OpenFolderTool(launcher),
            OpenWebsiteTool(launcher),
            AppStatusTool(catalog, process_controller),
            CloseAppTool(catalog, process_controller),
            ListFolderTool(validator),
            PathExistsTool(validator),
            CreateFolderTool(validator),
            RenamePathTool(validator),
            MovePathTool(validator),
        )
    )
    tools.extend(extra_tools)
    registry = ToolRegistry(
        tuple(tools),
        known_folders=KnownFolderResolver(home),
    )
    return Assistant(CommandRouter(registry, catalog), registry, provider)


def test_plan_rejects_fewer_than_two_or_more_than_three_actions() -> None:
    launcher = FakeLauncher()
    # 1 action passed directly to action plan
    plan_one = ActionPlan.__new__(ActionPlan)
    object.__setattr__(plan_one, 'actions', (ToolAction('open_app', {'app_name': 'Spotify'}),))
    provider = FakeProvider(IntentResult(IntentKind.ACTION_PLAN, plan=plan_one))
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open one')
    assert not res.success
    assert 'between 2 and 3' in res.result.message
    assert launcher.apps == []


def test_plan_preflight_rejection_executes_zero_actions() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('open_app', {'app_name': 'Chrome'}),
                ToolAction('open_app', {'app_name': 'NonExistentApplication123'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open Spotify, Chrome and fake')
    assert not res.success
    assert 'Action plan could not be prepared at step 3' in res.result.message
    assert launcher.apps == []


def test_plan_preflight_rejects_unknown_tool() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('unregistered_tool', {'arg': 'val'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open Spotify and unknown')
    assert not res.success
    assert 'Action plan could not be prepared at step 2' in res.result.message
    assert launcher.apps == []


def test_plan_preflight_rejects_destructive_action() -> None:
    launcher = FakeLauncher()
    # Create a mock destructive tool
    class FakeDestructiveTool:
        name = 'wipe_disk'
        def prepare(self, args):
            return ToolPreparation('val', ToolArguments((('target', args['target']),)))
        def execute(self, val):
            return ToolResult(True, 'wiped', RiskLevel.DESTRUCTIVE)

    destructive_def = ToolDefinition(
        'wipe_disk',
        'Dangerous',
        (string_argument('target', 'target'),),
        FakeDestructiveTool(),
        RiskLevel.DESTRUCTIVE,
        lambda args: 'Wipe disk',
    )
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('wipe_disk', {'target': 'all'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, extra_tools=(destructive_def,))

    res = assistant.handle('Open Spotify and wipe')
    assert not res.success
    assert 'cannot contain destructive actions' in res.result.message
    assert launcher.apps == []


def test_plan_preflight_rejects_more_than_one_sensitive_action() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify', 'Chrome'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('close_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Chrome'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle('Close Spotify and close Chrome')
    assert not res.success
    assert 'at most one sensitive action' in res.result.message
    assert controller.close_calls == []


def test_safe_two_step_plan_executes_in_order_and_aggregates_result() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Chrome'}),
                ToolAction('open_app', {'app_name': 'Spotify'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open Chrome and Spotify')
    assert res.success
    assert [app.display_name for app in launcher.apps] == ['Chrome', 'Spotify']
    assert res.result.risk_level == RiskLevel.SAFE
    assert 'Completed 2 actions:' in res.result.message
    assert '1. Opening Chrome.' in res.result.message
    assert '2. Opening Spotify.' in res.result.message
    assert provider.calls == ['Open Chrome and Spotify']


def test_safe_three_step_plan_executes_in_order() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Chrome'}),
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('open_website', {'url': 'https://example.com'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open Chrome, Spotify, and website')
    assert res.success
    assert [app.display_name for app in launcher.apps] == ['Chrome', 'Spotify']
    assert launcher.websites == ['https://example.com']
    assert 'Completed 3 actions:' in res.result.message
    assert '1. Opening Chrome.' in res.result.message
    assert '2. Opening Spotify.' in res.result.message
    assert '3. Opening website: https://example.com' in res.result.message


def test_plan_stops_on_runtime_failure_without_rollback() -> None:
    launcher = FakeLauncher()
    # Make launcher fail when launching Spotify
    orig_launch = launcher.launch_app
    def fail_on_spotify(app):
        if app.display_name == 'Spotify':
            from desktop_assistant.launcher import LaunchError
            raise LaunchError('Spotify launch crashed.')
        return orig_launch(app)
    launcher.launch_app = fail_on_spotify

    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Chrome'}),
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('open_website', {'url': 'https://example.com'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open Chrome, Spotify, and website')
    assert not res.success
    # Chrome executed
    assert [app.display_name for app in launcher.apps] == ['Chrome']
    # Step 3 website never executed
    assert launcher.websites == []
    assert 'Plan stopped at step 2 of 3: Spotify launch crashed.' in res.result.message
    assert 'Remaining steps were not run.' in res.result.message


def test_plan_cancellation_before_execution() -> None:
    launcher = FakeLauncher()
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Chrome'}),
                ToolAction('open_app', {'app_name': 'Spotify'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)
    token = CancellationToken()
    token.cancel()

    res = assistant.handle('Open Chrome and Spotify', cancellation_token=token)
    assert not res.success
    assert res.result.message == 'Request was cancelled.'
    assert launcher.apps == []


def test_plan_cancellation_between_steps() -> None:
    launcher = FakeLauncher()
    token = CancellationToken()
    orig_launch = launcher.launch_app
    def launch_and_cancel(app):
        orig_launch(app)
        token.cancel()
    launcher.launch_app = launch_and_cancel

    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Chrome'}),
                ToolAction('open_app', {'app_name': 'Spotify'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)

    res = assistant.handle('Open Chrome and Spotify', cancellation_token=token)
    assert not res.success
    assert [app.display_name for app in launcher.apps] == ['Chrome']
    assert 'Plan cancelled at step 2 of 2. 1 action(s) completed before cancellation.' in res.result.message


def test_plan_with_sensitive_step_in_middle_pauses_confirms_resumes() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
                ToolAction('open_app', {'app_name': 'Chrome'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    # Step 1: Handle request -> pauses at step 2
    res = assistant.handle('Open Spotify, close Spotify, open Chrome')
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    req = res.confirmation
    assert req is not None
    assert "Close all open windows of:" in req.summary
    assert "Spotify" in req.summary
    assert req.plan_context is not None
    assert req.plan_context.step_index == 2
    assert req.plan_context.total_steps == 3
    assert req.plan_context.completed_summaries == ('Opening Spotify.',)

    # Step 1 ran, step 2 and 3 did not run yet
    assert [app.display_name for app in launcher.apps] == ['Spotify']
    assert controller.close_calls == []
    assert assistant.has_pending_confirmation()
    assert len(provider.calls) == 1

    # While pending, new handle requests are rejected
    blocked = assistant.handle('new command')
    assert not blocked.success
    assert 'Confirm or cancel the pending action' in blocked.result.message

    # Step 2: Confirm sensitive step
    final = assistant.confirm(req.confirmation_id)
    assert final.kind is AssistantResponseKind.COMPLETED
    assert final.success
    assert final.result.risk_level == RiskLevel.SENSITIVE
    assert [app.display_name for app in controller.close_calls] == ['Spotify']
    assert [app.display_name for app in launcher.apps] == ['Spotify', 'Chrome']
    assert not assistant.has_pending_confirmation()
    assert 'Completed 3 actions:' in final.result.message
    assert '1. Opening Spotify.' in final.result.message
    assert '2. Requested all open Spotify windows to close.' in final.result.message
    assert '3. Opening Chrome.' in final.result.message
    # No second LLM provider call!
    assert len(provider.calls) == 1


def test_plan_with_sensitive_step_cancelled() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
                ToolAction('open_app', {'app_name': 'Chrome'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle('Open Spotify, close Spotify, open Chrome')
    req = res.confirmation
    assert req is not None

    cancel_res = assistant.cancel(req.confirmation_id)
    assert cancel_res.success
    assert 'Plan cancelled at step 2 of 3. 1 action(s) completed before cancellation. Remaining actions were discarded.' in cancel_res.result.message
    assert controller.close_calls == []
    assert [app.display_name for app in launcher.apps] == ['Spotify']
    assert not assistant.has_pending_confirmation()


def test_plan_with_sensitive_step_at_beginning() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('close_app', {'app_name': 'Spotify'}),
                ToolAction('open_app', {'app_name': 'Chrome'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle('Close Spotify and open Chrome')
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    req = res.confirmation
    assert req.plan_context.step_index == 1
    assert req.plan_context.total_steps == 2
    assert req.plan_context.completed_summaries == ()
    assert controller.close_calls == []
    assert launcher.apps == []

    final = assistant.confirm(req.confirmation_id)
    assert final.success
    assert [app.display_name for app in controller.close_calls] == ['Spotify']
    assert [app.display_name for app in launcher.apps] == ['Chrome']


def test_plan_with_sensitive_step_at_end() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle('Open Spotify and close Spotify')
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    req = res.confirmation
    assert req.plan_context.step_index == 2
    assert [app.display_name for app in launcher.apps] == ['Spotify']
    assert controller.close_calls == []

    final = assistant.confirm(req.confirmation_id)
    assert final.success
    assert [app.display_name for app in controller.close_calls] == ['Spotify']


def test_plan_confirmation_wrong_id_leaves_plan_pending() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle('Open and close Spotify')
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED

    wrong_res = assistant.confirm('wrong-id')
    assert not wrong_res.success
    assert 'That confirmation is no longer valid.' in wrong_res.result.message
    assert assistant.has_pending_confirmation()


def test_plan_shutdown_cleans_pending_plan() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle('Open and close Spotify')
    assert assistant.has_pending_confirmation()

    assistant.shutdown()
    assert not assistant.has_pending_confirmation()


def test_cli_plan_confirmation_shows_step_and_confirms() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
                ToolAction('open_app', {'app_name': 'Chrome'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)
    res = assistant.handle('Open Spotify, close Spotify, open Chrome')

    output: list[str] = []
    present_response(assistant, res, input_fn=lambda _prompt: 'y', output_fn=output.append)

    assert any('Plan step 2 of 3' in line for line in output)
    assert any('Completed: Opening Spotify.' in line for line in output)
    assert any('Completed 3 actions:' in line for line in output)
    assert [app.display_name for app in controller.close_calls] == ['Spotify']
    assert [app.display_name for app in launcher.apps] == ['Spotify', 'Chrome']


def test_cli_plan_confirmation_cancels() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({'Spotify'})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction('open_app', {'app_name': 'Spotify'}),
                ToolAction('close_app', {'app_name': 'Spotify'}),
                ToolAction('open_app', {'app_name': 'Chrome'}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)
    res = assistant.handle('Open Spotify, close Spotify, open Chrome')

    output: list[str] = []
    present_response(assistant, res, input_fn=lambda _prompt: 'n', output_fn=output.append)

    assert any('Plan step 2 of 3' in line for line in output)
    assert any('Plan cancelled at step 2 of 3.' in line for line in output)
    assert controller.close_calls == []
    assert [app.display_name for app in launcher.apps] == ['Spotify']
