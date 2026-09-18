from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from desktop_assistant.config import AppCatalog
from desktop_assistant.launcher import LaunchError, SystemLauncher
from desktop_assistant.models import RiskLevel, ToolResult


class OpenAppTool:
    name = "open_app"
    risk_level = RiskLevel.SAFE

    def __init__(self, launcher: SystemLauncher, catalog: AppCatalog) -> None:
        self._launcher = launcher
        self._catalog = catalog

    def run(self, app_name: str) -> ToolResult:
        app = self._catalog.resolve(app_name)
        if app is None:
            supported = ", ".join(self._catalog.names())
            return ToolResult(
                False,
                f"I can only open these applications: {supported}.",
                self.risk_level,
            )

        try:
            self._launcher.launch_app(app)
        except LaunchError as exc:
            return ToolResult(False, str(exc), self.risk_level)

        return ToolResult(
            True,
            f"Opening {app.display_name}.",
            self.risk_level,
            {"app": app.display_name},
        )


class OpenFolderTool:
    name = "open_folder"
    risk_level = RiskLevel.SAFE
    _blocked_device_prefixes = ("\\\\.\\", "\\\\?\\GLOBALROOT")

    def __init__(self, launcher: SystemLauncher) -> None:
        self._launcher = launcher

    def run(self, raw_path: str) -> ToolResult:
        cleaned = raw_path.strip().strip('"')
        if not cleaned:
            return ToolResult(False, "Please provide a folder path.", self.risk_level)
        if cleaned.upper().startswith(tuple(p.upper() for p in self._blocked_device_prefixes)):
            return ToolResult(False, "Windows device paths are not allowed.", self.risk_level)

        try:
            path = Path(cleaned).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return ToolResult(False, "That folder does not exist or is not accessible.", self.risk_level)

        if not path.is_dir():
            return ToolResult(False, "The supplied path is not a folder.", self.risk_level)

        try:
            self._launcher.open_folder(path)
        except LaunchError as exc:
            return ToolResult(False, str(exc), self.risk_level)

        return ToolResult(
            True,
            f"Opening folder: {path}",
            self.risk_level,
            {"path": str(path)},
        )


class OpenWebsiteTool:
    name = "open_website"
    risk_level = RiskLevel.SAFE

    def __init__(self, launcher: SystemLauncher) -> None:
        self._launcher = launcher

    def run(self, raw_url: str) -> ToolResult:
        url = raw_url.strip().strip('"')
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return ToolResult(False, "That URL is not valid.", self.risk_level)

        if parsed.scheme.casefold() not in {"http", "https"}:
            return ToolResult(False, "Only http and https URLs are allowed.", self.risk_level)
        if not parsed.hostname or parsed.username or parsed.password:
            return ToolResult(False, "That URL is not valid.", self.risk_level)
        if port is not None and not 1 <= port <= 65535:
            return ToolResult(False, "That URL contains an invalid port.", self.risk_level)
        if re.search(r"[\x00-\x20\x7f]", url):
            return ToolResult(False, "URLs cannot contain spaces or control characters.", self.risk_level)

        safe_url = urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, parsed.fragment))
        try:
            self._launcher.open_website(safe_url)
        except LaunchError as exc:
            return ToolResult(False, str(exc), self.risk_level)

        return ToolResult(
            True,
            f"Opening website: {safe_url}",
            self.risk_level,
            {"url": safe_url},
        )
