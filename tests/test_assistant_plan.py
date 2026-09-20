from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import MagicMock
import pytest

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.assistant import Assistant
from desktop_assistant.cancellation import CancellationToken
from desktop_assistant.cli import present_response
from desktop_assistant.config import AppCatalog
from desktop_assistant.confirmation import ConfirmationManager
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
from desktop_assistant.media_control import MediaControlTool, VolumeControlTool
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
from conftest import FakeLauncher, FakeMediaController, FakeProcessController


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
    confirmation_manager: ConfirmationManager | None = None,
    media_controller: FakeMediaController | None = None,
) -> Assistant:
    catalog = AppCatalog()
    validator = FilesystemPathValidator()
    process_controller = process_controller or FakeProcessController()
    media_controller = media_controller or FakeMediaController()
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
            VolumeControlTool(media_controller),
            MediaControlTool(media_controller),
        )
    )
    tools.extend(extra_tools)
    registry = ToolRegistry(
        tuple(tools),
        known_folders=KnownFolderResolver(home),
        confirmation_manager=confirmation_manager,
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


class BlockingSensitiveTool:
    name = "blocking_sensitive"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = False

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        return ToolPreparation(None, arguments)

    def execute(self, prepared_value: object) -> ToolResult:
        self.entered.set()
        self.release.wait(timeout=3.0)
        self.completed = True
        return ToolResult(True, "Sensitive action completed.", RiskLevel.SENSITIVE)


class BlockingSafeTool:
    name = "blocking_safe"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = False

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        return ToolPreparation(None, arguments)

    def execute(self, prepared_value: object) -> ToolResult:
        self.entered.set()
        self.release.wait(timeout=3.0)
        self.completed = True
        return ToolResult(True, "Safe action completed.", RiskLevel.SAFE)


class BlockingProvider:
    def __init__(self, result: IntentResult) -> None:
        self.result = result
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []

    def resolve(self, request: str) -> IntentResult:
        self.calls.append(request)
        self.entered.set()
        self.release.wait(timeout=3.0)
        return self.result


def test_plan_confirmation_expiry_clears_pending_plan_and_allows_new_requests() -> None:
    now = [100.0]
    manager = ConfirmationManager(lifetime_seconds=120, clock=lambda: now[0])
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
                ToolAction("open_app", {"app_name": "Chrome"}),
            )
        )
    )
    assistant = make_test_assistant(
        launcher, provider, process_controller=controller, confirmation_manager=manager
    )

    res = assistant.handle("Open Spotify, close Spotify, open Chrome")
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert [app.display_name for app in launcher.apps] == ["Spotify"]
    assert assistant.has_pending_confirmation()

    # Advance clock past expiry
    now[0] = 230.0

    # Assistant.has_pending_confirmation() should be False and pending plan cleared
    assert not assistant.has_pending_confirmation()

    # Next request succeeds and is not blocked
    provider.result = IntentResult.action_plan(
        (
            ToolAction("open_app", {"app_name": "Chrome"}),
            ToolAction("open_app", {"app_name": "Chrome"}),
        )
    )
    res2 = assistant.handle("Open Chrome and open Chrome")
    assert res2.success


def test_plan_confirm_after_expiry_rejects_and_runs_zero_actions() -> None:
    now = [100.0]
    manager = ConfirmationManager(lifetime_seconds=120, clock=lambda: now[0])
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
                ToolAction("open_app", {"app_name": "Chrome"}),
            )
        )
    )
    assistant = make_test_assistant(
        launcher, provider, process_controller=controller, confirmation_manager=manager
    )

    res = assistant.handle("Open Spotify, close Spotify, open Chrome")
    req = res.confirmation
    assert req is not None
    assert [app.display_name for app in launcher.apps] == ["Spotify"]
    assert controller.close_calls == []

    # Advance clock past expiry
    now[0] = 230.0

    # Confirm attempt
    confirm_res = assistant.confirm(req.confirmation_id)
    assert not confirm_res.success
    assert "expired" in confirm_res.result.message.casefold() or "no longer valid" in confirm_res.result.message.casefold()
    assert controller.close_calls == []
    assert [app.display_name for app in launcher.apps] == ["Spotify"]
    assert not assistant.has_pending_confirmation()


def test_plan_cancel_after_expiry_rejects_and_runs_zero_actions() -> None:
    now = [100.0]
    manager = ConfirmationManager(lifetime_seconds=120, clock=lambda: now[0])
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
                ToolAction("open_app", {"app_name": "Chrome"}),
            )
        )
    )
    assistant = make_test_assistant(
        launcher, provider, process_controller=controller, confirmation_manager=manager
    )

    res = assistant.handle("Open Spotify, close Spotify, open Chrome")
    req = res.confirmation
    assert req is not None

    now[0] = 230.0

    cancel_res = assistant.cancel(req.confirmation_id)
    assert not cancel_res.success
    assert "expired" in cancel_res.result.message.casefold() or "no longer valid" in cancel_res.result.message.casefold()
    assert controller.close_calls == []
    assert not assistant.has_pending_confirmation()


def test_plan_expiry_after_safe_step_does_not_rollback_and_discards_remaining() -> None:
    now = [100.0]
    manager = ConfirmationManager(lifetime_seconds=120, clock=lambda: now[0])
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
                ToolAction("open_app", {"app_name": "Chrome"}),
            )
        )
    )
    assistant = make_test_assistant(
        launcher, provider, process_controller=controller, confirmation_manager=manager
    )

    assistant.handle("Open Spotify, close Spotify, open Chrome")
    assert [app.display_name for app in launcher.apps] == ["Spotify"]

    # Expiry occurs
    now[0] = 300.0

    # New request executes without touching or rolling back step 1
    provider.result = IntentResult.action_plan(
        (
            ToolAction("open_app", {"app_name": "Notepad"}),
            ToolAction("open_app", {"app_name": "Notepad"}),
        )
    )
    res2 = assistant.handle("Open Notepad twice")
    assert res2.success
    # Completed safe action Spotify was not undone, sensitive close Spotify was never executed, step 3 Chrome was discarded
    assert [app.display_name for app in launcher.apps] == ["Spotify", "Notepad", "Notepad"]
    assert controller.close_calls == []


def test_shutdown_during_active_confirmed_sensitive_step_allows_sensitive_but_prevents_safe_continuation() -> None:
    launcher = FakeLauncher()
    blocking_sensitive = BlockingSensitiveTool()
    sensitive_def = ToolDefinition(
        name="blocking_sensitive",
        description="Blocks during execution.",
        arguments=(string_argument("target", "Target."),),
        implementation=blocking_sensitive,
        risk_level=RiskLevel.SENSITIVE,
        confirmation_summary=lambda _args: "Execute blocking sensitive action",
        confirmation_warning="Sensitive action warning",
    )
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("blocking_sensitive", {"target": "foo"}),
                ToolAction("open_app", {"app_name": "Chrome"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, extra_tools=(sensitive_def,))

    res = assistant.handle("Open Spotify, block sensitive, open Chrome")
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    req = res.confirmation
    assert req is not None
    assert [app.display_name for app in launcher.apps] == ["Spotify"]

    confirm_result: list[AssistantResponse] = []
    thread = threading.Thread(
        target=lambda: confirm_result.append(assistant.confirm(req.confirmation_id)),
        daemon=True,
    )
    thread.start()
    assert blocking_sensitive.entered.wait(timeout=2.0)

    # Shutdown called while sensitive action is actively executing
    assistant.shutdown()
    # Release the sensitive action to finish
    blocking_sensitive.release.set()
    thread.join(timeout=2.0)

    assert len(confirm_result) == 1
    # The active sensitive action completed cleanly!
    assert blocking_sensitive.completed is True
    # The subsequent safe action (Chrome) was NOT executed!
    assert [app.display_name for app in launcher.apps] == ["Spotify"]
    # Result indicates cancellation before step 3
    assert not confirm_result[0].success
    assert "step 3" in confirm_result[0].result.message


def test_shutdown_during_provider_resolution_executes_zero_plan_steps() -> None:
    launcher = FakeLauncher()
    provider = BlockingProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("open_app", {"app_name": "Chrome"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider)  # type: ignore[arg-type]

    thread_result: list[AssistantResponse] = []
    thread = threading.Thread(
        target=lambda: thread_result.append(assistant.handle("Open both")),
        daemon=True,
    )
    thread.start()
    assert provider.entered.wait(timeout=2.0)

    assistant.shutdown()
    provider.release.set()
    thread.join(timeout=2.0)

    assert len(thread_result) == 1
    assert not thread_result[0].success
    assert "shutting down" in thread_result[0].result.message.casefold() or "cancelled" in thread_result[0].result.message.casefold()
    assert launcher.apps == []


def test_shutdown_between_safe_plan_steps_stops_remaining() -> None:
    launcher = FakeLauncher()
    blocking_safe = BlockingSafeTool()
    safe_def = ToolDefinition(
        name="blocking_safe",
        description="Blocks during execution.",
        arguments=(string_argument("target", "Target."),),
        implementation=blocking_safe,
        risk_level=RiskLevel.SAFE,
        confirmation_summary=lambda _args: "Execute blocking safe action",
    )
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("blocking_safe", {"target": "bar"}),
                ToolAction("open_app", {"app_name": "Spotify"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, extra_tools=(safe_def,))

    thread_result: list[AssistantResponse] = []
    thread = threading.Thread(
        target=lambda: thread_result.append(assistant.handle("Run safe then open")),
        daemon=True,
    )
    thread.start()
    assert blocking_safe.entered.wait(timeout=2.0)

    assistant.shutdown()
    blocking_safe.release.set()
    thread.join(timeout=2.0)

    assert len(thread_result) == 1
    assert not thread_result[0].success
    assert blocking_safe.completed is True
    assert launcher.apps == []


def test_shutdown_while_waiting_for_plan_confirmation_clears_pending_plan() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle("Open Spotify and close Spotify")
    assert res.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    req = res.confirmation
    assert req is not None
    assert assistant.has_pending_confirmation()

    assistant.shutdown()
    assert not assistant.has_pending_confirmation()

    confirm_res = assistant.confirm(req.confirmation_id)
    assert not confirm_res.success
    cancel_res = assistant.cancel(req.confirmation_id)
    assert not cancel_res.success
    assert controller.close_calls == []


def test_tool_action_arguments_are_copy_owned_and_immutable() -> None:
    from types import MappingProxyType

    raw = {"app_name": "Spotify"}
    action = ToolAction("open_app", raw)

    assert isinstance(action.arguments, MappingProxyType)
    assert action.arguments["app_name"] == "Spotify"

    # Mutating raw dict does not affect action.arguments
    raw["app_name"] = "Malicious"
    assert action.arguments["app_name"] == "Spotify"

    # Mutating action.arguments raises TypeError
    with pytest.raises(TypeError):
        action.arguments["app_name"] = "Other"  # type: ignore[index]


def test_double_confirm_executes_sensitive_action_once() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle("Open and close Spotify")
    req = res.confirmation
    assert req is not None

    first = assistant.confirm(req.confirmation_id)
    assert first.success
    assert len(controller.close_calls) == 1

    second = assistant.confirm(req.confirmation_id)
    assert not second.success
    assert len(controller.close_calls) == 1


def test_cancel_then_confirm_executes_zero_sensitive_actions() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle("Open and close Spotify")
    req = res.confirmation
    assert req is not None

    cancel_res = assistant.cancel(req.confirmation_id)
    assert cancel_res.success
    assert len(controller.close_calls) == 0

    confirm_res = assistant.confirm(req.confirmation_id)
    assert not confirm_res.success
    assert len(controller.close_calls) == 0


def test_confirm_then_cancel_has_no_second_effect() -> None:
    launcher = FakeLauncher()
    controller = FakeProcessController({"Spotify"})
    provider = FakeProvider(
        IntentResult.action_plan(
            (
                ToolAction("open_app", {"app_name": "Spotify"}),
                ToolAction("close_app", {"app_name": "Spotify"}),
            )
        )
    )
    assistant = make_test_assistant(launcher, provider, process_controller=controller)

    res = assistant.handle("Open and close Spotify")
    req = res.confirmation
    assert req is not None

    confirm_res = assistant.confirm(req.confirmation_id)
    assert confirm_res.success

    cancel_res = assistant.cancel(req.confirmation_id)
    assert not cancel_res.success

