from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.config import AppCatalog, AppDefinition
from desktop_assistant.filesystem import FilesystemPathValidator
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    RenamePathTool,
)
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.media_control import (
    MediaAction,
    MediaControlTool,
    VolumeAction,
    VolumeControlTool,
)
from desktop_assistant.process_control import CloseRequestResult
from desktop_assistant.safety import SafetyPolicy
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


class FakeLauncher:
    def __init__(self) -> None:
        self.apps: list[AppDefinition] = []
        self.folders: list[Path] = []
        self.websites: list[str] = []

    def launch_app(self, app: AppDefinition) -> None:
        self.apps.append(app)

    def open_folder(self, path: Path) -> None:
        self.folders.append(path)

    def open_website(self, url: str) -> None:
        self.websites.append(url)


class FakeProcessController:
    def __init__(self, running: set[str] | None = None) -> None:
        self.running = {name.casefold() for name in (running or set())}
        self.status_calls: list[AppDefinition] = []
        self.close_calls: list[AppDefinition] = []
        self.requested_window_count = 1

    def is_running(self, app: AppDefinition) -> bool:
        self.status_calls.append(app)
        return app.display_name.casefold() in self.running

    def request_close(self, app: AppDefinition) -> CloseRequestResult:
        self.close_calls.append(app)
        return CloseRequestResult(
            app.display_name.casefold() in self.running,
            self.requested_window_count,
        )


from desktop_assistant.system_status import (
    BatteryInfo,
    DiskInfo,
    MemoryInfo,
    SystemStatusTool,
)


class FakeMediaController:
    def __init__(self) -> None:
        self.volume_actions: list[VolumeAction] = []
        self.media_actions: list[MediaAction] = []

    def send_volume(self, action: VolumeAction) -> None:
        self.volume_actions.append(action)

    def send_media(self, action: MediaAction) -> None:
        self.media_actions.append(action)


class FakeSystemStatusCollector:
    def __init__(
        self,
        cpu_percent: float = 15.0,
        memory: MemoryInfo | None = None,
        battery: BatteryInfo | None = None,
        disk: DiskInfo | None = None,
    ) -> None:
        self.cpu_percent = cpu_percent
        self.memory = memory or MemoryInfo(
            used_bytes=8 * (1024 ** 3),
            total_bytes=16 * (1024 ** 3),
            percent=50.0,
        )
        self.battery = battery or BatteryInfo(
            has_battery=True,
            percent=80,
            is_charging=True,
            ac_connected=True,
        )
        self.disk = disk or DiskInfo(
            drive="C:",
            total_bytes=500 * (1024 ** 3),
            used_bytes=250 * (1024 ** 3),
            free_bytes=250 * (1024 ** 3),
            percent=50.0,
        )

    def get_cpu_percent(self) -> float:
        return self.cpu_percent

    def get_memory_info(self) -> MemoryInfo:
        return self.memory

    def get_battery_info(self) -> BatteryInfo:
        return self.battery

    def get_disk_info(self) -> DiskInfo:
        return self.disk


from desktop_assistant.projects import (
    OpenProjectTool,
    ProjectCatalog,
    RunProjectTaskTool,
    TaskExecutionResult,
)


class FakeVSCodeLauncher:
    def __init__(self) -> None:
        self.opened_directories: list[Path] = []

    def open_directory(self, path: Path) -> None:
        self.opened_directories.append(path)


class FakeProjectTaskRunner:
    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "================== 325 passed, 2 skipped in 12.66s ==================",
        stderr: str = "",
        timed_out: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.calls: list[dict[str, object]] = []

    def run_task(
        self,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float = 180.0,
    ) -> TaskExecutionResult:
        self.calls.append({
            "command": command,
            "cwd": cwd,
            "timeout_seconds": timeout_seconds,
        })
        return TaskExecutionResult(
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


from desktop_assistant.window_input import WindowInputTool, InputAction
from desktop_assistant.ui_perception import UIInspectTool, UIInspection
from desktop_assistant.ui_action import (
    PreparedControlIdentity,
    PreparedUIAction,
    UIAction,
    UIActionTool,
)
from desktop_assistant.windows import (
    FocusWindowTool,
    WindowInfo,
    WindowInfoTool,
)


class FakeWindowController:
    def __init__(
        self,
        windows: tuple[WindowInfo, ...] = (),
        active_window: WindowInfo | None = None,
        focus_succeeds: bool = True,
    ) -> None:
        self.windows = list(windows)
        self.active_window = active_window
        self.focus_succeeds = focus_succeeds
        self.restored_handles: list[int] = []
        self.focused_handles: list[int] = []
        self.valid_handles: set[int] = {w.handle for w in windows}

    def visible_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(self.windows)

    def get_window_info(self, handle: int) -> WindowInfo | None:
        return next((w for w in self.windows if w.handle == handle and handle in self.valid_handles), None)

    def get_foreground_window(self) -> WindowInfo | None:
        return self.active_window

    def is_window_valid(self, handle: int, expected_process_id: int) -> bool:
        if handle not in self.valid_handles:
            return False
        for w in self.windows:
            if w.handle == handle:
                return w.process_id == expected_process_id
        return False

    def is_minimized(self, handle: int) -> bool:
        for w in self.windows:
            if w.handle == handle:
                return w.minimized
        return False

    def restore_window(self, handle: int) -> bool:
        self.restored_handles.append(handle)
        return True

    def set_foreground_window(self, handle: int) -> bool:
        if self.focus_succeeds:
            self.focused_handles.append(handle)
            return True
        return False


class FakeInputController:
    def __init__(self) -> None:
        self.calls: list[tuple[InputAction, str | None]] = []

    def send(self, action, value, verify_target) -> None:
        from desktop_assistant.window_input import InputError
        if not verify_target():
            raise InputError("Target verification failed. No input sent.")
        self.calls.append((action, value))


class FakeEditableControlResolver:
    def __init__(self) -> None:
        self.focus_calls: list[tuple[int, int]] = []
        self.focus_checks = 0
        self.focused = True
        self.error: Exception | None = None
        self.on_focus = None

    @contextmanager
    def focus(self, handle: int, process_id: int):
        self.focus_calls.append((handle, process_id))
        if self.error is not None:
            raise self.error
        if self.on_focus is not None:
            self.on_focus()
        yield self

    def is_focused(self) -> bool:
        self.focus_checks += 1
        return self.focused


class FakeUIInspector:
    def __init__(self, inspection: UIInspection | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.inspection = inspection or UIInspection((), False)

    def inspect(self, handle: int, process_id: int) -> UIInspection:
        self.calls.append((handle, process_id))
        return self.inspection


class FakeUIActionController:
    def __init__(self, resolve_result: PreparedControlIdentity | str | None = None) -> None:
        self.resolve_result = resolve_result
        self.resolve_calls: list[tuple[int, int, str, UIAction]] = []
        self.execute_calls: list[PreparedUIAction] = []
        self.execute_result: ToolResult | bool = True

    def resolve_control(
        self,
        handle: int,
        process_id: int,
        control_name: str,
        action: UIAction,
    ) -> PreparedControlIdentity | str:
        self.resolve_calls.append((handle, process_id, control_name, action))
        if self.resolve_result is not None:
            return self.resolve_result
        return PreparedControlIdentity(
            control_type="button",
            name=control_name,
            automation_id=f"auto_{control_name.lower()}",
            runtime_id=(42, handle, 100),
        )

    def execute_action(self, target: PreparedUIAction) -> ToolResult | bool:
        self.execute_calls.append(target)
        return self.execute_result


from desktop_assistant.visual_perception import (
    NormalizedVisualBounds,
    VisualInspectTool,
    VisualPerceptionProvider,
    VisualTargetResult,
    VisualTargetStatus,
    VisualTargetTool,
    WindowCapture,
    WindowCaptureBackend,
)


class FakeWindowCaptureBackend:
    def __init__(
        self,
        capture: WindowCapture | None = None,
        failed: bool = False,
        error: Exception | None = None,
    ) -> None:
        if failed:
            self.capture = None
        elif capture is not None:
            self.capture = capture
        else:
            import io
            from PIL import Image
            with Image.new("RGB", (800, 600), "white") as image, io.BytesIO() as output:
                image.save(output, format="PNG")
                self.capture = WindowCapture(png_bytes=output.getvalue(), width=800, height=600)
        self.error = error
        self.calls: list[int] = []

    def capture_window(self, handle: int) -> WindowCapture | None:
        self.calls.append(handle)
        if self.error is not None:
            raise self.error
        return self.capture


class FakeVisualPerceptionProvider:
    def locate_control(self, png_bytes, target):
        return self.locate_target(png_bytes, target)

    def refine_control(self, png_bytes, target):
        return self.refine_target(png_bytes, target)

    def __init__(
        self,
        observation: str = "A visible window with buttons and text.",
        target_result: VisualTargetResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.observation = observation
        self.target_result = target_result
        self.error = error
        self.calls: list[tuple[bytes, str]] = []
        self.target_calls: list[tuple[bytes, str]] = []
        self.refinement_calls: list[tuple[bytes, str]] = []

    def refine_target(self, png_bytes: bytes, target: str) -> VisualTargetResult:
        self.refinement_calls.append((png_bytes, target))
        return self.target_result or VisualTargetResult(
            VisualTargetStatus.FOUND, target, f"Visible {target} control",
            NormalizedVisualBounds(100, 100, 200, 200), 0.95,
        )

    def inspect(self, png_bytes: bytes, goal: str) -> str:
        self.calls.append((png_bytes, goal))
        if self.error is not None:
            raise self.error
        return self.observation

    def locate_target(self, png_bytes: bytes, target: str) -> VisualTargetResult:
        self.target_calls.append((png_bytes, target))
        if self.error is not None:
            raise self.error
        if self.target_result is not None:
            return self.target_result
        return VisualTargetResult(
            status=VisualTargetStatus.FOUND,
            label=target,
            description=f"Visible {target} control",
            bounds=NormalizedVisualBounds(left=100, top=100, right=200, bottom=200),
            confidence=0.95,
        )


from desktop_assistant.visual_click import VisualClickTool, WindowRectangle


class FakeForegroundClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeMouseClickController:
    def __init__(self):
        self.rectangle = WindowRectangle(100, 200, 900, 800)
        self.root = 101
        self.position = (0, 0)
        self.moves = []
        self.clicks = 0
        self.rect_reads = []
        self.on_move = None
        self.on_click = None

    def get_window_rect(self, handle):
        self.rect_reads.append(handle)
        return self.rectangle

    def root_at_point(self, point):
        return self.root

    def move_cursor(self, point):
        self.moves.append(point)
        self.position = point
        if self.on_move:
            self.on_move()
        return True

    def get_cursor_pos(self):
        return self.position

    def left_click(self, verify_target):
        from desktop_assistant.visual_click import MouseClickError
        if self.on_click:
            self.on_click()
        if not verify_target():
            raise MouseClickError("The prepared target changed. No click sent.")
        self.clicks += 1


def make_registry(
    launcher: FakeLauncher,
    *,
    safety_policy: SafetyPolicy | None = None,
    home: Path | None = None,
    process_controller: FakeProcessController | None = None,
    media_controller: FakeMediaController | None = None,
    system_status_collector: FakeSystemStatusCollector | None = None,
    project_catalog: ProjectCatalog | None = None,
    vscode_launcher: FakeVSCodeLauncher | None = None,
    task_runner: FakeProjectTaskRunner | None = None,
    window_controller: FakeWindowController | None = None,
    input_controller: FakeInputController | None = None,
    editable_control_resolver: FakeEditableControlResolver | None = None,
    ui_inspector: FakeUIInspector | None = None,
    ui_action_controller: FakeUIActionController | None = None,
    window_capture_backend: WindowCaptureBackend | None = None,
    visual_perception_provider: VisualPerceptionProvider | None = None,
    mouse_click_controller: FakeMouseClickController | None = None,
) -> ToolRegistry:
    catalog = AppCatalog()
    validator = FilesystemPathValidator()
    process_controller = process_controller or FakeProcessController()
    media_controller = media_controller or FakeMediaController()
    system_status_collector = system_status_collector or FakeSystemStatusCollector()
    project_catalog = project_catalog or ProjectCatalog()
    vscode_launcher = vscode_launcher or FakeVSCodeLauncher()
    task_runner = task_runner or FakeProjectTaskRunner()
    window_controller = window_controller or FakeWindowController()
    capture_backend = window_capture_backend or FakeWindowCaptureBackend()
    vision_provider = visual_perception_provider or FakeVisualPerceptionProvider()
    foreground_clock = FakeForegroundClock()
    return ToolRegistry(
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
            SystemStatusTool(system_status_collector),
            OpenProjectTool(project_catalog, vscode_launcher),
            RunProjectTaskTool(project_catalog, task_runner),
            WindowInfoTool(window_controller),
            FocusWindowTool(window_controller, catalog),
            WindowInputTool(window_controller, input_controller or FakeInputController(), catalog,
                            editable_control_resolver or FakeEditableControlResolver()),
            UIInspectTool(window_controller, catalog, ui_inspector or FakeUIInspector()),
            UIActionTool(window_controller, catalog, ui_action_controller or FakeUIActionController()),
            VisualInspectTool(
                window_controller,
                catalog,
                capture_backend,
                vision_provider,
            ),
            VisualTargetTool(
                window_controller,
                catalog,
                capture_backend,
                vision_provider,
            ),
            VisualClickTool(window_controller,
                            VisualTargetTool(window_controller, catalog, capture_backend, vision_provider),
                            mouse_click_controller or FakeMouseClickController(),
                            clock=foreground_clock.clock, sleeper=foreground_clock.sleep),
        ),
        safety_policy=safety_policy,
        known_folders=KnownFolderResolver(home),
    )
