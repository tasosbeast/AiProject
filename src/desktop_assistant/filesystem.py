from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


_IS_WINDOWS = os.name == "nt"
_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x0400,
)


class FilesystemValidationError(ValueError):
    """A concise, user-safe filesystem validation failure."""


@dataclass(frozen=True, slots=True)
class EntryIdentity:
    device: int
    inode: int
    file_type: int

    @classmethod
    def capture(cls, path: Path) -> "EntryIdentity":
        metadata = path.stat(follow_symlinks=False)
        return cls(metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


@dataclass(frozen=True, slots=True)
class FilesystemSafetyPolicy:
    """Application-owned protected Windows locations for mutation tools."""

    protected_trees: tuple[Path, ...]
    protected_exact: tuple[Path, ...]

    @classmethod
    def from_environment(cls) -> "FilesystemSafetyPolicy":
        tree_values = (
            os.environ.get("SystemRoot", r"C:\Windows"),
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramData", r"C:\ProgramData"),
        )
        trees = tuple(cls._resolved(Path(value)) for value in tree_values if value)
        drive_roots = {Path(tree.anchor) for tree in trees if tree.anchor}
        system_drive = os.environ.get("SystemDrive")
        if system_drive:
            drive_roots.add(Path(f"{system_drive}\\"))
        return cls(trees, tuple(cls._resolved(path) for path in drive_roots))

    def is_protected(self, path: Path) -> bool:
        if self.is_drive_root(path):
            return True
        normalized = self._resolved(path)
        if self.is_drive_root(normalized):
            return True
        if any(normalized == root or normalized.parent == root for root in self.protected_exact):
            return True
        return any(normalized == root or root in normalized.parents for root in self.protected_trees)

    @staticmethod
    def is_drive_root(path: Path) -> bool:
        """Return whether *path* is any absolute Windows drive root."""

        windows_path = PureWindowsPath(str(path).replace("/", "\\"))
        return bool(
            windows_path.drive
            and windows_path.root
            and windows_path == PureWindowsPath(windows_path.anchor)
        )

    @staticmethod
    def _resolved(path: Path) -> Path:
        try:
            return path.resolve(strict=False)
        except OSError:
            return path.absolute()


class FilesystemPathValidator:
    """Reusable normalization and safety checks for local filesystem tools."""

    _device_prefixes = ("\\\\.\\", "\\\\?\\", "\\??\\")
    _reserved_names = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    _invalid_component_chars = re.compile(r"[<>:\"|?*\x00-\x1f]")

    def __init__(self, policy: FilesystemSafetyPolicy | None = None) -> None:
        self._policy = policy or FilesystemSafetyPolicy.from_environment()

    def existing_directory(self, raw_path: str) -> Path:
        path = self._existing(raw_path, mutation=False)
        if not path.is_dir():
            raise FilesystemValidationError("The supplied path is not a folder.")
        return path

    def query_path(self, raw_path: str) -> Path:
        lexical = self._lexical(raw_path)
        try:
            return lexical.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise FilesystemValidationError("That path is not valid or accessible.") from exc

    def mutation_source(self, raw_path: str) -> Path:
        path = self._existing(raw_path, mutation=True)
        if not path.is_file() and not path.is_dir():
            raise FilesystemValidationError("Only ordinary files and folders are supported.")
        self._reject_protected(path)
        return path

    def mutation_destination(self, raw_path: str) -> Path:
        lexical = self._lexical(raw_path, require_absolute=True)
        if os.path.lexists(lexical):
            raise FilesystemValidationError("Destination already exists.")
        parent_lexical = lexical.parent
        try:
            self._reject_reparse_chain(parent_lexical)
        except FileNotFoundError as exc:
            raise FilesystemValidationError("Destination parent does not exist.") from exc
        try:
            parent = parent_lexical.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FilesystemValidationError("Destination parent does not exist.") from exc
        if not parent.is_dir():
            raise FilesystemValidationError("Destination parent is not a folder.")
        destination = parent / lexical.name
        self._reject_protected(parent)
        self._reject_protected(destination)
        return destination

    def assert_identity(self, path: Path, expected: EntryIdentity, *, label: str) -> None:
        try:
            current = EntryIdentity.capture(path)
        except OSError as exc:
            raise FilesystemValidationError(
                f"The {label} changed before the confirmed action could run."
            ) from exc
        if current != expected:
            raise FilesystemValidationError(
                f"The {label} changed before the confirmed action could run."
            )

    def assert_same_volume(self, source: Path, destination_parent: Path) -> None:
        if source.drive and destination_parent.drive:
            if source.drive.casefold() != destination_parent.drive.casefold():
                raise FilesystemValidationError("Cross-volume moves are not supported.")
        try:
            source_device = source.stat(follow_symlinks=False).st_dev
            parent_device = destination_parent.stat(follow_symlinks=False).st_dev
        except OSError as exc:
            raise FilesystemValidationError("The filesystem location could not be validated.") from exc
        if source_device != parent_device:
            raise FilesystemValidationError("Cross-volume moves are not supported.")

    def _existing(self, raw_path: str, *, mutation: bool) -> Path:
        lexical = self._lexical(raw_path, require_absolute=mutation)
        if mutation:
            try:
                self._reject_reparse_chain(lexical)
            except FileNotFoundError as exc:
                raise FilesystemValidationError("Source path does not exist.") from exc
        try:
            return lexical.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            message = (
                "Source path does not exist."
                if mutation
                else "That path does not exist or is not accessible."
            )
            raise FilesystemValidationError(message) from exc

    def _lexical(self, raw_path: str, *, require_absolute: bool = False) -> Path:
        cleaned = raw_path.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == '"':
            cleaned = cleaned[1:-1].strip()
        if not cleaned:
            raise FilesystemValidationError("Please provide a path.")
        windows_text = cleaned.replace("/", "\\")
        upper = windows_text.upper()
        if upper.startswith(self._device_prefixes) or upper.startswith("\\\\"):
            raise FilesystemValidationError("Windows device and network paths are not supported.")
        pure = PureWindowsPath(windows_text)
        if pure.drive and not pure.root:
            raise FilesystemValidationError("Drive-relative paths are not supported.")
        self._validate_components(pure)
        try:
            path = Path(cleaned).expanduser()
        except (OSError, RuntimeError) as exc:
            raise FilesystemValidationError("That path is not valid.") from exc
        if require_absolute and not path.is_absolute():
            raise FilesystemValidationError(
                "Sensitive filesystem actions require an exact absolute path."
            )
        return path.absolute()

    def _validate_components(self, path: PureWindowsPath) -> None:
        for component in path.parts:
            if component in {path.anchor, "\\", "/"}:
                continue
            if component in {".", ".."}:
                raise FilesystemValidationError("Relative path traversal is not supported.")
            trimmed = component.rstrip(" .")
            if trimmed != component:
                raise FilesystemValidationError(
                    "Windows paths cannot end components with dots or spaces."
                )
            stem = trimmed.split(".", 1)[0].upper()
            if not trimmed or stem in self._reserved_names:
                raise FilesystemValidationError("That path contains a reserved Windows name.")
            if self._invalid_component_chars.search(component):
                raise FilesystemValidationError("That path contains invalid Windows characters.")

    def _reject_protected(self, path: Path) -> None:
        if self._policy.is_protected(path):
            raise FilesystemValidationError("This protected system location cannot be modified.")

    def _reject_reparse_chain(self, path: Path) -> None:
        chain: list[Path] = []
        current = path
        while True:
            chain.append(current)
            if current == current.parent:
                break
            current = current.parent

        for current in reversed(chain):
            if self._is_reparse_point(current):
                raise FilesystemValidationError(
                    "This filesystem path contains a symbolic link or junction "
                    "and cannot be modified safely."
                )

    @classmethod
    def _is_reparse_point(cls, path: Path) -> bool:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise FilesystemValidationError(
                "The filesystem path could not be validated safely."
            ) from exc

        if stat.S_ISLNK(metadata.st_mode):
            return True
        if not _IS_WINDOWS:
            return False

        attributes = getattr(metadata, "st_file_attributes", None)
        if attributes is None:
            raise FilesystemValidationError(
                "The filesystem path could not be validated safely."
            )
        return bool(attributes & _WINDOWS_REPARSE_POINT_ATTRIBUTE)
