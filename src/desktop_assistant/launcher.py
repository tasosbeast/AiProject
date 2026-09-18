from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from desktop_assistant.config import AppDefinition


class LaunchError(RuntimeError):
    """Raised when Windows cannot launch a validated target."""


class SystemLauncher(Protocol):
    def launch_app(self, app: AppDefinition) -> None: ...

    def open_folder(self, path: Path) -> None: ...

    def open_website(self, url: str) -> None: ...


class WindowsSystemLauncher:
    """The narrow boundary around Windows process and shell APIs."""

    def launch_app(self, app: AppDefinition) -> None:
        errors: list[str] = []
        for target in app.targets:
            value = os.path.expandvars(target.value)
            try:
                if target.kind == "uri":
                    os.startfile(value)  # type: ignore[attr-defined]
                    return

                executable = self._find_executable(value)
                if executable is None:
                    continue
                subprocess.Popen(
                    [executable],
                    shell=False,
                    close_fds=True,
                )
                return
            except OSError as exc:
                errors.append(str(exc))

        detail = f" ({'; '.join(errors)})" if errors else ""
        raise LaunchError(f"{app.display_name} is not installed or could not be found{detail}.")

    @staticmethod
    def _find_executable(value: str) -> str | None:
        if os.path.isabs(value):
            return value if Path(value).is_file() else None
        return shutil.which(value)

    def open_folder(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            raise LaunchError(f"Windows could not open the folder: {exc}.") from exc

    def open_website(self, url: str) -> None:
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except OSError as exc:
            raise LaunchError(f"Windows could not open the website: {exc}.") from exc

