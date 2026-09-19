from __future__ import annotations

import re
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
        cleaned = value.strip().strip('"')
        parts = re.split(r"[\\/]", cleaned, maxsplit=1)
        head = parts[0]
        normalized = " ".join(head.casefold().split())
        relative_name = self._folder_names.get(normalized)
        if relative_name is None:
            return value
        base = self._home / relative_name if relative_name else self._home
        if len(parts) == 1:
            return str(base)
        remainder = parts[1]
        relative = Path(remainder.replace("\\", "/"))
        return str(base / relative)
