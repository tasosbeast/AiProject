from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_DATA_DIRECTORY = Path("AiProject") / "AI Assistant"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Stable paths for source development and frozen Windows builds."""

    is_frozen: bool
    application_root: Path
    config_directory: Path
    log_directory: Path
    env_file: Path

    @classmethod
    def detect(
        cls,
        *,
        frozen: bool | None = None,
        executable: str | Path | None = None,
        module_file: str | Path | None = None,
        local_app_data: str | Path | None = None,
    ) -> "RuntimePaths":
        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        source_file = Path(module_file or __file__).resolve()
        if is_frozen:
            application_root = Path(executable or sys.executable).resolve().parent
            local_root_value = local_app_data or os.getenv("LOCALAPPDATA")
            if local_root_value:
                local_root = Path(local_root_value).expanduser().resolve()
            else:
                local_root = (Path.home() / "AppData" / "Local").resolve()
            config_directory = local_root / APP_DATA_DIRECTORY
        else:
            application_root = source_file.parents[2]
            config_directory = application_root
        return cls(
            is_frozen=is_frozen,
            application_root=application_root,
            config_directory=config_directory,
            log_directory=config_directory / "logs",
            env_file=config_directory / ".env.local",
        )

    def ensure_user_directories(self) -> None:
        """Create only app-owned runtime directories, never the install directory."""

        self.config_directory.mkdir(parents=True, exist_ok=True)
        self.log_directory.mkdir(parents=True, exist_ok=True)
