from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path

from desktop_assistant.filesystem import (
    EntryIdentity,
    FilesystemPathValidator,
    FilesystemValidationError,
)
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult


@dataclass(frozen=True, slots=True)
class PreparedPath:
    path: Path


@dataclass(frozen=True, slots=True)
class PreparedCreateFolder:
    path: Path
    parent_identity: EntryIdentity


@dataclass(frozen=True, slots=True)
class PreparedTransfer:
    source: Path
    destination: Path
    source_identity: EntryIdentity
    destination_parent_identity: EntryIdentity


class ListFolderTool:
    name = "list_folder"
    risk_level = RiskLevel.SAFE
    maximum_entries = 100

    def __init__(self, validator: FilesystemPathValidator) -> None:
        self._validator = validator

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        try:
            path = self._validator.existing_directory(arguments["path"])
        except FilesystemValidationError as exc:
            return self._rejected(str(exc))
        return ToolPreparation(
            PreparedPath(path),
            ToolArguments((("path", str(path)),)),
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedPath):
            return self._rejected("The prepared folder listing is invalid.")
        try:
            path = self._validator.existing_directory(str(prepared_value.path))
            entries: list[tuple[bool, str]] = []
            truncated = False
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if len(entries) >= self.maximum_entries:
                        truncated = True
                        break
                    entries.append((entry.is_dir(follow_symlinks=False), entry.name))
        except FilesystemValidationError as exc:
            return self._rejected(str(exc))
        except OSError:
            return self._rejected("That folder could not be listed.")

        entries.sort(key=lambda item: (not item[0], item[1].casefold()))
        lines = [
            f"[Folder] {name}" if is_directory else f"[File] {name}"
            for is_directory, name in entries
        ]
        if not lines:
            lines.append("(empty folder)")
        if truncated:
            lines.append(f"… listing truncated after {self.maximum_entries} entries.")
        return ToolResult(
            True,
            f"Contents of {path}:\n" + "\n".join(lines),
            self.risk_level,
            {"path": str(path), "entry_count": len(entries), "truncated": truncated},
        )

    def run(self, path: str) -> ToolResult:
        prepared = self.prepare(ToolArguments((("path", path),)))
        return prepared if isinstance(prepared, ToolResult) else self.execute(prepared.execution_value)

    def _rejected(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)


class PathExistsTool:
    name = "path_exists"
    risk_level = RiskLevel.SAFE

    def __init__(self, validator: FilesystemPathValidator) -> None:
        self._validator = validator

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        try:
            path = self._validator.query_path(arguments["path"])
        except FilesystemValidationError as exc:
            return self._rejected(str(exc))
        return ToolPreparation(PreparedPath(path), ToolArguments((("path", str(path)),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedPath):
            return self._rejected("The prepared path check is invalid.")
        try:
            path = self._validator.query_path(str(prepared_value.path))
            exists = path.exists()
            is_file = exists and path.is_file()
            is_directory = exists and path.is_dir()
        except (FilesystemValidationError, OSError):
            return self._rejected("That path could not be checked.")
        kind = "folder" if is_directory else "file" if is_file else "other entry"
        message = f"Path exists ({kind}): {path}" if exists else f"Path does not exist: {path}"
        return ToolResult(
            True,
            message,
            self.risk_level,
            {
                "path": str(path),
                "exists": exists,
                "is_file": is_file,
                "is_directory": is_directory,
            },
        )

    def run(self, path: str) -> ToolResult:
        prepared = self.prepare(ToolArguments((("path", path),)))
        return prepared if isinstance(prepared, ToolResult) else self.execute(prepared.execution_value)

    def _rejected(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)


class CreateFolderTool:
    name = "create_folder"
    risk_level = RiskLevel.SENSITIVE

    def __init__(self, validator: FilesystemPathValidator) -> None:
        self._validator = validator

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        try:
            path = self._validator.mutation_destination(arguments["path"])
            parent_identity = EntryIdentity.capture(path.parent)
        except (FilesystemValidationError, OSError) as exc:
            return self._rejected(str(exc))
        return ToolPreparation(
            PreparedCreateFolder(path, parent_identity),
            ToolArguments((("path", str(path)),)),
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedCreateFolder):
            return self._rejected("The prepared folder creation is invalid.")
        try:
            path = self._validator.mutation_destination(str(prepared_value.path))
            if path != prepared_value.path:
                raise FilesystemValidationError(
                    "The destination changed before the confirmed action could run."
                )
            self._validator.assert_identity(
                path.parent,
                prepared_value.parent_identity,
                label="destination parent",
            )
            path.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            return self._rejected("Destination already exists.")
        except FilesystemValidationError as exc:
            return self._rejected(str(exc))
        except OSError:
            return self._rejected("The folder could not be created.")
        return ToolResult(
            True,
            f"Created folder: {path}",
            self.risk_level,
            {"path": str(path)},
        )

    def _rejected(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)


class _TransferTool:
    risk_level = RiskLevel.SENSITIVE
    operation_name: str

    def __init__(self, validator: FilesystemPathValidator) -> None:
        self._validator = validator

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        try:
            source = self._validator.mutation_source(arguments["source"])
            if self._validator.query_path(arguments["destination"]) == source:
                raise FilesystemValidationError("Source and destination must be different.")
            destination = self._validator.mutation_destination(arguments["destination"])
            self._validate_semantics(source, destination)
            self._validator.assert_same_volume(source, destination.parent)
            prepared = PreparedTransfer(
                source,
                destination,
                EntryIdentity.capture(source),
                EntryIdentity.capture(destination.parent),
            )
        except (FilesystemValidationError, OSError) as exc:
            return self._rejected(str(exc))
        return ToolPreparation(
            prepared,
            ToolArguments((("source", str(source)), ("destination", str(destination)))),
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedTransfer):
            return self._rejected(f"The prepared {self.operation_name} is invalid.")
        try:
            source = self._validator.mutation_source(str(prepared_value.source))
        except FilesystemValidationError:
            return self._rejected("The source changed before the confirmed action could run.")
        try:
            destination = self._validator.mutation_destination(str(prepared_value.destination))
            if source != prepared_value.source or destination != prepared_value.destination:
                raise FilesystemValidationError("The paths changed before the confirmed action could run.")
            self._validate_semantics(source, destination)
            self._validator.assert_identity(source, prepared_value.source_identity, label="source")
            self._validator.assert_identity(
                destination.parent,
                prepared_value.destination_parent_identity,
                label="destination parent",
            )
            self._validator.assert_same_volume(source, destination.parent)
            os.rename(source, destination)
        except FileExistsError:
            return self._rejected("Destination already exists.")
        except FilesystemValidationError as exc:
            return self._rejected(str(exc))
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return self._rejected("Cross-volume moves are not supported.")
            return self._rejected(f"The {self.operation_name} could not be completed.")
        verb = "Renamed" if self.operation_name == "rename" else "Moved"
        return ToolResult(
            True,
            f"{verb}:\n{source}\n→\n{destination}",
            self.risk_level,
            {"source": str(source), "destination": str(destination)},
        )

    def _validate_semantics(self, source: Path, destination: Path) -> None:
        if source == destination:
            raise FilesystemValidationError("Source and destination must be different.")
        if source.is_dir() and (
            destination.parent == source or source in destination.parent.parents
        ):
            raise FilesystemValidationError("A folder cannot be moved inside itself.")

    def _rejected(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)


class RenamePathTool(_TransferTool):
    name = "rename_path"
    operation_name = "rename"

    def _validate_semantics(self, source: Path, destination: Path) -> None:
        super()._validate_semantics(source, destination)
        if source.parent != destination.parent:
            raise FilesystemValidationError("Rename must keep the item in its current folder.")


class MovePathTool(_TransferTool):
    name = "move_path"
    operation_name = "move"

    def _validate_semantics(self, source: Path, destination: Path) -> None:
        super()._validate_semantics(source, destination)
        if source.parent == destination.parent:
            raise FilesystemValidationError("Use rename when the destination is in the same folder.")
