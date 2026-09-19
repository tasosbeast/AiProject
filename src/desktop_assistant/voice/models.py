from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioRecording:
    path: Path
    duration_ms: int

    def is_usable(self, minimum_bytes: int = 512) -> bool:
        try:
            return (
                self.duration_ms > 0
                and self.path.is_file()
                and self.path.stat().st_size >= minimum_bytes
            )
        except OSError:
            return False

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # Shutdown cleanup is best-effort; the OS can briefly retain a media handle.
            pass


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    data: bytes
    suffix: str = ".wav"
