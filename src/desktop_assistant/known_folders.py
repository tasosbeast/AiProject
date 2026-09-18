from __future__ import annotations

from pathlib import Path


class KnownFolderResolver:
    """Resolves a small, explicit set of ordinary user-folder names."""

    _folder_names = {
        "home": "",
        "desktop": "Desktop",
        "documents": "Documents",
        "downloads": "Downloads",
        "music": "Music",
        "pictures": "Pictures",
        "videos": "Videos",
    }

    def __init__(self, home: Path | None = None) -> None:
        self._home = home or Path.home()

    def resolve(self, value: str) -> str:
        normalized = " ".join(value.strip().casefold().split())
        relative_name = self._folder_names.get(normalized)
        if relative_name is None:
            return value
        return str(self._home / relative_name) if relative_name else str(self._home)
