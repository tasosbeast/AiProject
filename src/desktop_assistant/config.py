from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True, slots=True)
class LaunchTarget:
    """A predefined executable or Windows URI launch target."""

    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class AppDefinition:
    display_name: str
    aliases: tuple[str, ...]
    targets: tuple[LaunchTarget, ...]


SUPPORTED_APPS: tuple[AppDefinition, ...] = (
    AppDefinition(
        display_name="Chrome",
        aliases=("chrome", "google chrome"),
        targets=(
            LaunchTarget("executable", "chrome.exe"),
            LaunchTarget("executable", r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            LaunchTarget("executable", r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            LaunchTarget("executable", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ),
    ),
    AppDefinition(
        display_name="Spotify",
        aliases=("spotify",),
        targets=(
            LaunchTarget("executable", r"%APPDATA%\Spotify\Spotify.exe"),
            LaunchTarget("uri", "spotify:"),
        ),
    ),
    AppDefinition(
        display_name="VS Code",
        aliases=("vs code", "vscode", "visual studio code"),
        targets=(
            LaunchTarget("executable", "code.exe"),
            LaunchTarget("executable", "code"),
            LaunchTarget("executable", r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        ),
    ),
    AppDefinition(
        display_name="File Explorer",
        aliases=("file explorer", "explorer", "windows explorer"),
        targets=(LaunchTarget("executable", "explorer.exe"),),
    ),
    AppDefinition(
        display_name="Notepad",
        aliases=("notepad",),
        targets=(LaunchTarget("executable", "notepad.exe"),),
    ),
)


@dataclass(frozen=True, slots=True)
class Settings:
    log_level: str = "INFO"
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = "gpt-5.6-luna"
    openai_timeout_seconds: float = 15.0
    openai_max_retries: int = 1

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            log_level=os.getenv("ASSISTANT_LOG_LEVEL", "INFO").upper(),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load local values without replacing explicit process environment values."""

    if env_file is None:
        discovered = find_dotenv(".env.local", usecwd=True)
        if discovered:
            load_dotenv(discovered, override=False)
    else:
        path = Path(env_file)
        if path.is_file():
            load_dotenv(path, override=False)
    return Settings.from_environment()


class AppCatalog:
    """Resolves user-friendly names to predefined application definitions."""

    def __init__(self, apps: tuple[AppDefinition, ...] = SUPPORTED_APPS) -> None:
        self._apps = apps
        self._by_alias = {
            self._normalize(alias): app for app in apps for alias in app.aliases
        }

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().strip().split())

    def resolve(self, name: str) -> AppDefinition | None:
        return self._by_alias.get(self._normalize(name))

    def names(self) -> tuple[str, ...]:
        return tuple(app.display_name for app in self._apps)
