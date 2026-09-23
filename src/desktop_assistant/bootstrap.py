from __future__ import annotations

from dataclasses import dataclass

from desktop_assistant.app_tools import AppStatusTool, CloseAppTool
from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog, Settings, load_settings
from desktop_assistant.filesystem import FilesystemPathValidator
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    RenamePathTool,
)
from desktop_assistant.intent.provider import IntentProvider
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.launcher import SystemLauncher, WindowsSystemLauncher
from desktop_assistant.media_control import (
    MediaControlTool,
    MediaController,
    VolumeControlTool,
    WindowsMediaController,
)
from desktop_assistant.process_control import AppProcessController, WindowsAppProcessController
from desktop_assistant.projects import (
    OpenProjectTool,
    ProjectCatalog,
    ProjectTaskRunner,
    RunProjectTaskTool,
    SubprocessProjectTaskRunner,
    VSCodeLauncher,
    WindowsVSCodeLauncher,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.system_status import (
    SystemStatusCollector,
    SystemStatusTool,
    WindowsSystemStatusCollector,
)
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool
from desktop_assistant.voice.providers import SpeechProvider, TranscriptionProvider
from desktop_assistant.window_input import InputController, WindowInputTool, WindowsInputController
from desktop_assistant.windows import (
    FocusWindowTool,
    WindowController,
    WindowInfoTool,
    WindowsWindowController,
)


_AUTO_PROVIDER = object()


@dataclass(frozen=True, slots=True)
class VoiceServices:
    transcription: TranscriptionProvider | None
    speech: SpeechProvider | None


def build_assistant(
    settings: Settings | None = None,
    launcher: SystemLauncher | None = None,
    intent_provider: IntentProvider | None | object = _AUTO_PROVIDER,
    known_folders: KnownFolderResolver | None = None,
    process_controller: AppProcessController | None = None,
    media_controller: MediaController | None = None,
    system_status_collector: SystemStatusCollector | None = None,
    project_catalog: ProjectCatalog | None = None,
    vscode_launcher: VSCodeLauncher | None = None,
    task_runner: ProjectTaskRunner | None = None,
    window_controller: WindowController | None = None,
    input_controller: InputController | None = None,
) -> Assistant:
    """Compose the production assistant shared by every user interface."""

    settings = settings or load_settings()
    launcher = launcher or WindowsSystemLauncher()
    catalog = AppCatalog()
    process_controller = process_controller or WindowsAppProcessController()
    media_controller = media_controller or WindowsMediaController()
    system_status_collector = system_status_collector or WindowsSystemStatusCollector()
    project_catalog = project_catalog or ProjectCatalog()
    vscode_launcher = vscode_launcher or WindowsVSCodeLauncher()
    task_runner = task_runner or SubprocessProjectTaskRunner()
    window_controller = window_controller or WindowsWindowController()
    input_controller = input_controller or WindowsInputController()
    filesystem_validator = FilesystemPathValidator()
    registry = ToolRegistry(
        default_tool_definitions(
            OpenAppTool(launcher, catalog),
            OpenFolderTool(launcher),
            OpenWebsiteTool(launcher),
            AppStatusTool(catalog, process_controller),
            CloseAppTool(catalog, process_controller),
            ListFolderTool(filesystem_validator),
            PathExistsTool(filesystem_validator),
            CreateFolderTool(filesystem_validator),
            RenamePathTool(filesystem_validator),
            MovePathTool(filesystem_validator),
            VolumeControlTool(media_controller),
            MediaControlTool(media_controller),
            SystemStatusTool(system_status_collector),
            OpenProjectTool(project_catalog, vscode_launcher),
            RunProjectTaskTool(project_catalog, task_runner),
            WindowInfoTool(window_controller),
            FocusWindowTool(window_controller, catalog),
            WindowInputTool(window_controller, input_controller, catalog),
        ),
        known_folders=known_folders,
    )
    router = CommandRouter(registry, catalog)

    provider: IntentProvider | None
    if intent_provider is _AUTO_PROVIDER:
        provider = _build_openai_provider(settings, registry)
    else:
        provider = intent_provider  # type: ignore[assignment]
    return Assistant(router, registry, provider)


def _build_openai_provider(settings: Settings, registry: ToolRegistry) -> IntentProvider | None:
    if settings.openai_api_key is None:
        return None

    from desktop_assistant.intent.openai_provider import OpenAIIntentProvider

    return OpenAIIntentProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        tool_schemas=registry.schemas(),
        timeout_seconds=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )


def build_voice_services(settings: Settings | None = None) -> VoiceServices:
    """Build optional audio providers without making any startup API request."""

    settings = settings or load_settings()
    if settings.openai_api_key is None:
        return VoiceServices(None, None)

    from desktop_assistant.voice.openai_audio import (
        OpenAISpeechProvider,
        OpenAITranscriptionProvider,
    )

    transcription = OpenAITranscriptionProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_transcribe_model,
        timeout_seconds=settings.openai_audio_timeout_seconds,
        max_retries=settings.openai_audio_max_retries,
    )
    speech: SpeechProvider | None = None
    if settings.voice_output_enabled:
        speech = OpenAISpeechProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_tts_model,
            voice=settings.openai_tts_voice,
            timeout_seconds=settings.openai_audio_timeout_seconds,
            max_retries=settings.openai_audio_max_retries,
        )
    return VoiceServices(transcription, speech)
