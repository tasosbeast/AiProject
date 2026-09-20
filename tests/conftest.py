from __future__ import annotations

from pathlib import Path

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


def make_registry(
    launcher: FakeLauncher,
    *,
    safety_policy: SafetyPolicy | None = None,
    home: Path | None = None,
    process_controller: FakeProcessController | None = None,
    media_controller: FakeMediaController | None = None,
    system_status_collector: FakeSystemStatusCollector | None = None,
) -> ToolRegistry:
    catalog = AppCatalog()
    validator = FilesystemPathValidator()
    process_controller = process_controller or FakeProcessController()
    media_controller = media_controller or FakeMediaController()
    system_status_collector = system_status_collector or FakeSystemStatusCollector()
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
        ),
        safety_policy=safety_policy,
        known_folders=KnownFolderResolver(home),
    )
