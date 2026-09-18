from __future__ import annotations

from pathlib import Path

from desktop_assistant.config import AppDefinition


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

