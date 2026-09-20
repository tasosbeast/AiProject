from __future__ import annotations

import os
from pathlib import Path
import pytest

from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog
from desktop_assistant.confirmation import ConfirmationManager
from desktop_assistant.filesystem_tools import FilesystemPathValidator
from desktop_assistant.intent.models import ActionPlan, IntentKind, IntentResult, ToolAction
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.models import (
    AssistantResponseKind,
    RiskLevel,
    ToolArguments,
    ToolPreparation,
    ToolResult,
)
from desktop_assistant.projects import (
    OpenProjectTool,
    ProjectCatalog,
    RunProjectTaskTool,
    SubprocessProjectTaskRunner,
    TaskExecutionResult,
    TrustedProject,
    WindowsVSCodeLauncher,
    _extract_pytest_failure,
    _extract_pytest_summary,
    _truncate_output,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions

from conftest import (
    FakeLauncher,
    FakeMediaController,
    FakeProcessController,
    FakeProjectTaskRunner,
    FakeSystemStatusCollector,
    FakeVSCodeLauncher,
    make_registry,
)


class FakeProvider:
    def __init__(self, result: IntentResult | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    def resolve(self, request: str) -> IntentResult:
        self.calls.append(request)
        assert self.result is not None
        return self.result


def _build_test_assistant(
    catalog: ProjectCatalog,
    vscode_launcher: FakeVSCodeLauncher,
    task_runner: FakeProjectTaskRunner,
    provider: FakeProvider | None = None,
    home: Path | None = None,
) -> Assistant:
    fake_launcher = FakeLauncher()
    app_catalog = AppCatalog()
    registry = make_registry(
        fake_launcher,
        home=home,
        project_catalog=catalog,
        vscode_launcher=vscode_launcher,
        task_runner=task_runner,
    )
    router = CommandRouter(registry, app_catalog)
    return Assistant(router, registry, provider)


def test_project_catalog_default_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIPROJECT_PATH", raising=False)
    catalog = ProjectCatalog()

    project = catalog.get("AiProject")
    assert project is not None
    assert project.name == "AiProject"
    expected_root = (Path.home() / "Desktop" / "AiProject").resolve()
    assert project.root == expected_root
    assert project.venv_python == expected_root / ".venv" / "Scripts" / "python.exe"


def test_project_catalog_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom_root = tmp_path / "CustomAiProject"
    custom_root.mkdir()
    monkeypatch.setenv("AIPROJECT_PATH", str(custom_root))

    catalog = ProjectCatalog()
    project = catalog.get("AiProject")
    assert project is not None
    assert project.root == custom_root.resolve()


def test_project_catalog_unknown_project() -> None:
    catalog = ProjectCatalog()
    assert catalog.get("MaliciousProject") is None
    assert catalog.get("NonExistent") is None


def test_open_project_rejects_unknown_project() -> None:
    catalog = ProjectCatalog()
    launcher = FakeVSCodeLauncher()
    tool = OpenProjectTool(catalog, launcher)

    prep = tool.prepare(ToolArguments((("project_name", "UnknownProject"),)))
    assert isinstance(prep, ToolResult)
    assert not prep.success
    assert "Unknown project" in prep.message


def test_open_project_rejects_nonexistent_directory(tmp_path: Path) -> None:
    non_existent = tmp_path / "Does_Not_Exist"
    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", non_existent)})
    launcher = FakeVSCodeLauncher()
    tool = OpenProjectTool(catalog, launcher)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"),)))
    assert isinstance(prep, ToolResult)
    assert not prep.success
    assert "does not exist" in prep.message


def test_open_project_success(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    launcher = FakeVSCodeLauncher()
    tool = OpenProjectTool(catalog, launcher)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"),)))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert result.success
    assert "Opened AiProject in VS Code" in result.message
    assert launcher.opened_directories == [proj_dir]


def test_run_project_task_rejects_unsupported_task(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    runner = FakeProjectTaskRunner()
    tool = RunProjectTaskTool(catalog, runner)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"), ("task", "format"))))
    assert isinstance(prep, ToolResult)
    assert not prep.success
    assert "Unsupported task 'format'" in prep.message


def test_run_project_task_preflight_checks_missing_venv_python(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    # Dir exists, but .venv/Scripts/python.exe does not
    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    runner = FakeProjectTaskRunner()
    tool = RunProjectTaskTool(catalog, runner)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"), ("task", "tests"))))
    assert isinstance(prep, ToolResult)
    assert not prep.success
    assert "Virtualenv Python not found" in prep.message


def test_run_project_task_confirmation_summary(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    tool = RunProjectTaskTool(catalog, FakeProjectTaskRunner())

    summary = tool.confirmation_summary(ToolArguments((("project_name", "AiProject"), ("task", "tests"))))
    assert "AiProject" in summary
    assert "tests" in summary
    assert str(proj_dir / ".venv" / "Scripts" / "python.exe") in summary
    assert "-m pytest -vv -ra -W error" in summary


def test_run_project_task_success(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    python_exe = proj_dir / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("")

    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    runner = FakeProjectTaskRunner(
        exit_code=0,
        stdout="================== 325 passed, 2 skipped in 12.66s ==================",
    )
    tool = RunProjectTaskTool(catalog, runner)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"), ("task", "tests"))))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert result.success
    assert "Tests passed for AiProject: 325 passed, 2 skipped in 12.66s." in result.message
    assert len(runner.calls) == 1
    assert runner.calls[0]["cwd"] == proj_dir
    assert runner.calls[0]["command"] == (
        str(python_exe),
        "-m",
        "pytest",
        "-vv",
        "-ra",
        "-W",
        "error",
    )


def test_run_project_task_failure_summarizes_output(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    python_exe = proj_dir / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("")

    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    failed_stdout = (
        "tests/test_foo.py F\n"
        "=========================== FAILURES ===========================\n"
        "___________________________ test_bar ___________________________\n"
        "assert 1 == 2\n"
        "=================== short test summary info ====================\n"
        "FAILED tests/test_foo.py::test_bar - assert 1 == 2\n"
        "=================== 1 failed, 100 passed in 2.3s ==================="
    )
    runner = FakeProjectTaskRunner(
        exit_code=1,
        stdout=failed_stdout,
    )
    tool = RunProjectTaskTool(catalog, runner)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"), ("task", "tests"))))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert not result.success
    assert "Tests failed for AiProject (exit code 1):" in result.message
    assert "FAILED tests/test_foo.py::test_bar - assert 1 == 2" in result.message


def test_run_project_task_timeout(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    python_exe = proj_dir / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("")

    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    runner = FakeProjectTaskRunner(timed_out=True)
    tool = RunProjectTaskTool(catalog, runner)

    prep = tool.prepare(ToolArguments((("project_name", "AiProject"), ("task", "tests"))))
    assert isinstance(prep, ToolPreparation)

    result = tool.execute(prep.execution_value)
    assert not result.success
    assert "timed out after 180 seconds" in result.message


def test_output_helpers() -> None:
    assert _truncate_output("short", 50) == "short"
    long_str = "a" * 100
    truncated = _truncate_output(long_str, 20)
    assert truncated.startswith("a" * 20)
    assert "[truncated]" in truncated

    summary = _extract_pytest_summary("=== 15 passed, 1 warning in 1.2s ===")
    assert summary == "15 passed, 1 warning in 1.2s"

    failure = _extract_pytest_failure(
        "=== short test summary info ===\nFAILED test_x\n=== 1 failed ===",
        "",
    )
    assert failure == "FAILED test_x"


def test_plan_preflight_fails_whole_plan_if_task_invalid(tmp_path: Path) -> None:
    # Project dir exists, but venv python is missing.
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()

    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    vscode_launcher = FakeVSCodeLauncher()
    task_runner = FakeProjectTaskRunner()

    plan = ActionPlan((
        ToolAction("open_project", {"project_name": "AiProject"}),
        ToolAction("run_project_task", {"project_name": "AiProject", "task": "tests"}),
    ))
    provider = FakeProvider(IntentResult.action_plan(plan.actions))

    assistant = _build_test_assistant(catalog, vscode_launcher, task_runner, provider)
    response = assistant.handle("Άνοιξε το AiProject στο VS Code και τρέξε τα tests.")

    assert response.kind is AssistantResponseKind.COMPLETED
    assert not response.success
    assert "Virtualenv Python not found" in response.message
    # Preflight failure prevented step 1 from executing!
    assert vscode_launcher.opened_directories == []
    assert len(task_runner.calls) == 0


def test_plan_lifecycle_confirmation_and_execution(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    python_exe = proj_dir / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("")

    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    vscode_launcher = FakeVSCodeLauncher()
    task_runner = FakeProjectTaskRunner(
        exit_code=0,
        stdout="================== 325 passed in 10s ==================",
    )

    plan = ActionPlan((
        ToolAction("open_project", {"project_name": "AiProject"}),
        ToolAction("run_project_task", {"project_name": "AiProject", "task": "tests"}),
    ))
    provider = FakeProvider(IntentResult.action_plan(plan.actions))

    assistant = _build_test_assistant(catalog, vscode_launcher, task_runner, provider)
    response = assistant.handle("Άνοιξε το AiProject στο VS Code και τρέξε τα tests.")

    # Step 1 was SAFE so it executed immediately
    assert vscode_launcher.opened_directories == [proj_dir]
    # Step 2 was SENSITIVE so assistant paused for confirmation
    assert response.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    req = response.confirmation
    assert req is not None
    assert "AiProject" in req.summary
    assert "tests" in req.summary
    assert len(task_runner.calls) == 0
    assert len(provider.calls) == 1

    # Confirm executes step 2 without calling the intent provider again
    confirm_response = assistant.confirm(req.confirmation_id)
    assert confirm_response.kind is AssistantResponseKind.COMPLETED
    assert confirm_response.success
    assert "Tests passed for AiProject: 325 passed in 10s." in confirm_response.result.message
    assert len(task_runner.calls) == 1
    assert len(provider.calls) == 1  # No second provider call


def test_plan_lifecycle_cancellation(tmp_path: Path) -> None:
    proj_dir = tmp_path / "AiProject"
    proj_dir.mkdir()
    python_exe = proj_dir / ".venv" / "Scripts" / "python.exe"
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text("")

    catalog = ProjectCatalog({"AiProject": TrustedProject("AiProject", proj_dir)})
    vscode_launcher = FakeVSCodeLauncher()
    task_runner = FakeProjectTaskRunner()

    plan = ActionPlan((
        ToolAction("open_project", {"project_name": "AiProject"}),
        ToolAction("run_project_task", {"project_name": "AiProject", "task": "tests"}),
    ))
    provider = FakeProvider(IntentResult.action_plan(plan.actions))

    assistant = _build_test_assistant(catalog, vscode_launcher, task_runner, provider)
    response = assistant.handle("Άνοιξε το AiProject στο VS Code και τρέξε τα tests.")

    assert response.kind is AssistantResponseKind.CONFIRMATION_REQUIRED
    assert vscode_launcher.opened_directories == [proj_dir]
    req = response.confirmation
    assert req is not None

    cancel_response = assistant.cancel(req.confirmation_id)
    assert cancel_response.kind is AssistantResponseKind.COMPLETED
    assert cancel_response.success
    assert "Plan cancelled at step 2 of 2" in cancel_response.result.message
    assert len(task_runner.calls) == 0
    assert not assistant.has_pending_confirmation()


def test_deterministic_router_routes_open_project_and_run_tests() -> None:
    router = CommandRouter()

    decision1 = router.route_detailed("open aiproject")
    assert decision1.recognized
    assert decision1.action is not None
    assert decision1.action.tool_name == "open_project"
    assert decision1.action.arguments == {"project_name": "AiProject"}

    decision2 = router.route_detailed("run tests")
    assert decision2.recognized
    assert decision2.action is not None
    assert decision2.action.tool_name == "run_project_task"
    assert decision2.action.arguments == {"project_name": "AiProject", "task": "tests"}
