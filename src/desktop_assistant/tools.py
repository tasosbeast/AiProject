from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from desktop_assistant.config import AppCatalog, AppDefinition
from desktop_assistant.launcher import LaunchError, SystemLauncher
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult


class OpenAppTool:
    name = "open_app"
    risk_level = RiskLevel.SAFE

    def __init__(self, launcher: SystemLauncher, catalog: AppCatalog) -> None:
        self._launcher = launcher
        self._catalog = catalog

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        app_name = arguments["app_name"]
        app = self._catalog.resolve(app_name)
        if app is None:
            supported = ", ".join(self._catalog.names())
            return ToolResult(
                False,
                f"I can only open these applications: {supported}.",
                self.risk_level,
            )
        return ToolPreparation(app, ToolArguments((("app_name", app.display_name),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, AppDefinition):
            return self._invalid_prepared()
        if self._catalog.resolve(prepared_value.display_name) != prepared_value:
            return self._invalid_prepared()
        try:
            self._launcher.launch_app(prepared_value)
        except LaunchError as exc:
            return ToolResult(False, str(exc), self.risk_level)
        return ToolResult(
            True,
            f"Opening {prepared_value.display_name}.",
            self.risk_level,
            {"app": prepared_value.display_name},
        )

    def run(self, app_name: str) -> ToolResult:
        prepared = self.prepare(ToolArguments((("app_name", app_name),)))
        return prepared if isinstance(prepared, ToolResult) else self.execute(prepared.execution_value)

    def _invalid_prepared(self) -> ToolResult:
        return ToolResult(False, "The prepared application action is no longer valid.", self.risk_level)


class OpenFolderTool:
    name = "open_folder"
    risk_level = RiskLevel.SAFE
    _blocked_device_prefixes = ("\\\\.\\", "\\\\?\\GLOBALROOT")

    def __init__(self, launcher: SystemLauncher) -> None:
        self._launcher = launcher

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        raw_path = arguments["path"]
        path = self._validated_path(raw_path)
        if isinstance(path, ToolResult):
            return path
        return ToolPreparation(path, ToolArguments((("path", str(path)),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, Path):
            return self._invalid_prepared()
        # Final checks reduce TOCTOU exposure; confirmation cannot guarantee the
        # filesystem stayed unchanged between preparation and execution.
        path = self._validated_path(str(prepared_value))
        if isinstance(path, ToolResult) or path != prepared_value:
            return self._invalid_prepared()
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

    def run(self, raw_path: str) -> ToolResult:
        prepared = self.prepare(ToolArguments((("path", raw_path),)))
        return prepared if isinstance(prepared, ToolResult) else self.execute(prepared.execution_value)

    def _validated_path(self, raw_path: str) -> Path | ToolResult:
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
        return path

    def _invalid_prepared(self) -> ToolResult:
        return ToolResult(False, "The prepared folder action is no longer valid.", self.risk_level)


class OpenWebsiteTool:
    name = "open_website"
    risk_level = RiskLevel.SAFE

    def __init__(self, launcher: SystemLauncher) -> None:
        self._launcher = launcher

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        raw_url = arguments["url"]
        safe_url = self._validated_url(raw_url)
        if isinstance(safe_url, ToolResult):
            return safe_url
        return ToolPreparation(safe_url, ToolArguments((("url", safe_url),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, str):
            return self._invalid_prepared()
        safe_url = self._validated_url(prepared_value)
        if isinstance(safe_url, ToolResult) or safe_url != prepared_value:
            return self._invalid_prepared()
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

    def run(self, raw_url: str) -> ToolResult:
        prepared = self.prepare(ToolArguments((("url", raw_url),)))
        return prepared if isinstance(prepared, ToolResult) else self.execute(prepared.execution_value)

    def _validated_url(self, raw_url: str) -> str | ToolResult:
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
        return urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )

    def _invalid_prepared(self) -> ToolResult:
        return ToolResult(False, "The prepared website action is no longer valid.", self.risk_level)
