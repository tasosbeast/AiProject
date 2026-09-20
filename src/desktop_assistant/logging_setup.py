from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from desktop_assistant.runtime_paths import RuntimePaths


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_LOG_BYTES = 1_500_000
LOG_BACKUP_COUNT = 3


def configure_logging(level_name: str, paths: RuntimePaths) -> Path | None:
    """Configure bounded file logs for frozen builds and safe source logging."""

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT)

    if not paths.is_frozen:
        stream = getattr(sys, "stderr", None)
        if stream is not None:
            handler = logging.StreamHandler(stream)
            handler.setFormatter(formatter)
            root.addHandler(handler)
        else:
            root.addHandler(logging.NullHandler())
        return None

    try:
        paths.ensure_user_directories()
        log_path = paths.log_directory / "assistant.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        # A windowed executable may have no stderr. Logging must never prevent startup.
        root.addHandler(logging.NullHandler())
        return None
    handler.setFormatter(formatter)
    root.addHandler(handler)
    return log_path
