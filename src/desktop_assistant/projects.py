from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import MappingProxyType
from typing import Mapping, Protocol

from desktop_assistant.launcher import LaunchError
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrustedProject:
    name: str
    root: Path

    @property
    def venv_python(self) -> Path:
        return self.root / ".venv" / "Scripts" / "python.exe"

    def task_command(self, task: str) -> tuple[str, ...] | None:
        if task == "tests":
            return (
                str(self.venv_python),
                "-m",
                "pytest",
                "-vv",
                "-ra",
                "-W",
                "error",
            )
        return None


class ProjectCatalog:
    """Catalog of locally trusted projects authorized for assistant actions."""

    def __init__(self, projects: Mapping[str, TrustedProject] | None = None) -> None:
        if projects is None:
            self._projects = self._default_projects()
        else:
            self._projects = MappingProxyType(dict(projects))

    @staticmethod
    def _default_projects() -> MappingProxyType[str, TrustedProject]:
        env_path = os.environ.get("AIPROJECT_PATH")
        if env_path:
            ai_root = Path(env_path).resolve()
        else:
            ai_root = (Path.home() / "Desktop" / "AiProject").resolve()

        return MappingProxyType({
            "AiProject": TrustedProject(
                name="AiProject",
                root=ai_root,
            )
        })

    def get(self, name: str) -> TrustedProject | None:
        for project_name, project in self._projects.items():
            if project_name.casefold() == name.casefold():
                return project
        return None

    def names(self) -> tuple[str, ...]:
        return tuple(self._projects.keys())


class VSCodeLauncher(Protocol):
    def open_directory(self, path: Path) -> None: ...


class WindowsVSCodeLauncher:
    """Fixed, non-configurable launcher for VS Code directories."""

    _CANDIDATES: tuple[str, ...] = (
        "code.cmd",
        "code.exe",
        "code",
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
        r"%PROGRAMFILES(X86)%\Microsoft VS Code\Code.exe",
    )

    def open_directory(self, path: Path) -> None:
        for candidate in self._CANDIDATES:
            expanded = os.path.expandvars(candidate)
            exe = expanded if Path(expanded).is_file() else shutil.which(expanded)
            if exe is not None:
                try:
                    subprocess.Popen(
                        [exe, str(path)],
                        shell=False,
                        close_fds=True,
                    )
                    return
                except OSError:
                    continue
        raise LaunchError("VS Code executable could not be found or launched.")


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class ProjectTaskRunner(Protocol):
    def run_task(
        self,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float = 180.0,
    ) -> TaskExecutionResult: ...


class SubprocessProjectTaskRunner:
    """Direct subprocess executor with shell=False and strict timeout."""

    def run_task(
        self,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float = 180.0,
    ) -> TaskExecutionResult:
        try:
            proc = subprocess.run(
                list(command),
                cwd=str(cwd),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return TaskExecutionResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_str = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode() if exc.stdout else "")
            stderr_str = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode() if exc.stderr else "")
            return TaskExecutionResult(
                exit_code=-1,
                stdout=stdout_str,
                stderr=stderr_str,
                timed_out=True,
            )


@dataclass(frozen=True, slots=True)
class PreparedProjectTask:
    project_name: str
    task: str
    command: tuple[str, ...]
    cwd: Path


def _truncate_output(text: str, max_chars: int = 500) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + " ... [truncated]"


_PYTEST_SUMMARY_RE = re.compile(r"=+ ([\d]+ passed[^\n=]*) =+", re.IGNORECASE)
_PYTEST_FAIL_SUMMARY_RE = re.compile(r"=+ ([\d]+ failed[^\n=]*) =+", re.IGNORECASE)
_PYTEST_SHORT_INFO_RE = re.compile(r"=+ short test summary info =+\n(.*?)(?:\n=+|\Z)", re.DOTALL | re.IGNORECASE)


def _extract_pytest_summary(stdout: str) -> str | None:
    match = _PYTEST_SUMMARY_RE.search(stdout)
    if match:
        return match.group(1).strip()
    return None


def _extract_pytest_failure(stdout: str, stderr: str) -> str:
    short_match = _PYTEST_SHORT_INFO_RE.search(stdout)
    if short_match:
        return _truncate_output(short_match.group(1).strip(), 400)
    fail_match = _PYTEST_FAIL_SUMMARY_RE.search(stdout)
    if fail_match:
        return fail_match.group(1).strip()
    combined = (stdout + "\n" + stderr).strip()
    lines = [line for line in combined.splitlines() if line.strip()]
    tail = "\n".join(lines[-5:]) if lines else "Unknown test failure."
    return _truncate_output(tail, 400)


class OpenProjectTool:
    name = "open_project"
    risk_level = RiskLevel.SAFE
    allowed_projects = ("AiProject",)

    def __init__(self, catalog: ProjectCatalog, launcher: VSCodeLauncher) -> None:
        self._catalog = catalog
        self._launcher = launcher

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        project_name = arguments.get("project_name")
        project = self._catalog.get(str(project_name))
        if project is None or project.name not in self.allowed_projects:
            supported = ", ".join(self.allowed_projects)
            return ToolResult(
                False,
                f"Unknown project '{project_name}'. Supported projects: {supported}.",
                self.risk_level,
            )
        if not project.root.is_dir():
            return ToolResult(
                False,
                f"Project directory '{project.root}' does not exist.",
                self.risk_level,
            )
        return ToolPreparation(project, ToolArguments((("project_name", project.name),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, TrustedProject):
            return ToolResult(False, "The prepared project action is invalid.", self.risk_level)
        if not prepared_value.root.is_dir():
            return ToolResult(False, f"Project directory '{prepared_value.root}' does not exist.", self.risk_level)

        try:
            self._launcher.open_directory(prepared_value.root)
        except LaunchError as exc:
            return ToolResult(False, str(exc), self.risk_level)
        except Exception:
            logger.exception("Failed to open project in VS Code: %s", prepared_value.name)
            return ToolResult(False, f"Failed to open {prepared_value.name} in VS Code.", self.risk_level)

        return ToolResult(
            True,
            f"Opened {prepared_value.name} in VS Code.",
            self.risk_level,
            {"project": prepared_value.name, "path": str(prepared_value.root)},
        )


class RunProjectTaskTool:
    name = "run_project_task"
    risk_level = RiskLevel.SENSITIVE
    allowed_projects = ("AiProject",)
    allowed_tasks = ("tests",)

    def __init__(self, catalog: ProjectCatalog, runner: ProjectTaskRunner) -> None:
        self._catalog = catalog
        self._runner = runner

    def confirmation_summary(self, arguments: ToolArguments) -> str:
        project = self._catalog.get(str(arguments.get("project_name", "")))
        task = str(arguments.get("task", ""))
        cmd_str = ""
        if project is not None:
            cmd = project.task_command(task)
            if cmd is not None:
                cmd_str = " ".join(cmd)
        return (
            f"Run project task:\n"
            f"Project: {arguments.get('project_name')}\n"
            f"Task: {task}\n"
            f"Command: {cmd_str}"
        )

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        project_name = arguments.get("project_name")
        task = arguments.get("task")
        project = self._catalog.get(str(project_name))
        if project is None or project.name not in self.allowed_projects:
            supported = ", ".join(self.allowed_projects)
            return ToolResult(
                False,
                f"Unknown project '{project_name}'. Supported projects: {supported}.",
                self.risk_level,
            )
        if task not in self.allowed_tasks:
            supported = ", ".join(self.allowed_tasks)
            return ToolResult(
                False,
                f"Unsupported task '{task}'. Supported tasks for {project.name}: {supported}.",
                self.risk_level,
            )
        if not project.root.is_dir():
            return ToolResult(
                False,
                f"Project directory '{project.root}' does not exist.",
                self.risk_level,
            )
        if not project.venv_python.is_file():
            return ToolResult(
                False,
                f"Virtualenv Python not found at '{project.venv_python}'.",
                self.risk_level,
            )
        command = project.task_command(str(task))
        if command is None:
            return ToolResult(False, f"No command configured for task '{task}'.", self.risk_level)

        prepared = PreparedProjectTask(
            project_name=project.name,
            task=str(task),
            command=command,
            cwd=project.root,
        )
        return ToolPreparation(
            prepared,
            ToolArguments((("project_name", project.name), ("task", str(task)))),
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedProjectTask):
            return ToolResult(False, "The prepared task action is invalid.", self.risk_level)
        if not prepared_value.cwd.is_dir():
            return ToolResult(False, f"Project directory '{prepared_value.cwd}' does not exist.", self.risk_level)
        if not Path(prepared_value.command[0]).is_file():
            return ToolResult(False, f"Task executable '{prepared_value.command[0]}' does not exist.", self.risk_level)

        try:
            result = self._runner.run_task(
                prepared_value.command,
                prepared_value.cwd,
                timeout_seconds=180.0,
            )
        except Exception:
            logger.exception("Failed to run project task %s for %s", prepared_value.task, prepared_value.project_name)
            return ToolResult(False, f"Failed to run task '{prepared_value.task}'.", self.risk_level)

        if result.timed_out:
            return ToolResult(
                False,
                f"Task '{prepared_value.task}' for {prepared_value.project_name} timed out after 180 seconds.",
                self.risk_level,
                {"exit_code": -1, "timed_out": True},
            )

        if result.exit_code == 0:
            summary = _extract_pytest_summary(result.stdout)
            if summary:
                message = f"Tests passed for {prepared_value.project_name}: {summary}."
            else:
                message = f"Tests passed for {prepared_value.project_name}."
            return ToolResult(
                True,
                message,
                self.risk_level,
                {
                    "project": prepared_value.project_name,
                    "task": prepared_value.task,
                    "exit_code": 0,
                    "stdout": _truncate_output(result.stdout, 1000),
                },
            )

        failure_summary = _extract_pytest_failure(result.stdout, result.stderr)
        message = (
            f"Tests failed for {prepared_value.project_name} (exit code {result.exit_code}):\n"
            f"{failure_summary}"
        )
        return ToolResult(
            False,
            message,
            self.risk_level,
            {
                "project": prepared_value.project_name,
                "task": prepared_value.task,
                "exit_code": result.exit_code,
                "stdout": _truncate_output(result.stdout, 1000),
                "stderr": _truncate_output(result.stderr, 1000),
            },
        )
