from __future__ import annotations

from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog, Settings, load_settings
from desktop_assistant.intent.provider import IntentProvider
from desktop_assistant.known_folders import KnownFolderResolver
from desktop_assistant.launcher import SystemLauncher, WindowsSystemLauncher
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import ToolRegistry, default_tool_definitions
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


_AUTO_PROVIDER = object()


def build_assistant(
    settings: Settings | None = None,
    launcher: SystemLauncher | None = None,
    intent_provider: IntentProvider | None | object = _AUTO_PROVIDER,
    known_folders: KnownFolderResolver | None = None,
) -> Assistant:
    """Compose the production assistant shared by every user interface."""

    settings = settings or load_settings()
    launcher = launcher or WindowsSystemLauncher()
    catalog = AppCatalog()
    registry = ToolRegistry(
        default_tool_definitions(
            OpenAppTool(launcher, catalog),
            OpenFolderTool(launcher),
            OpenWebsiteTool(launcher),
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
