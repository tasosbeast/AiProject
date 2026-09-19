from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from desktop_assistant.confirmation import PreparedAction
from desktop_assistant.filesystem import (
    FilesystemPathValidator,
    FilesystemSafetyPolicy,
    FilesystemValidationError,
)
from desktop_assistant.filesystem_tools import (
    CreateFolderTool,
    ListFolderTool,
    MovePathTool,
    PathExistsTool,
    PreparedTransfer,
    RenamePathTool,
)
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolArguments, ToolResult

from conftest import FakeLauncher, make_registry


def confirm(registry: object, outcome: object) -> ToolResult:
    assert isinstance(outcome, ConfirmationRequest)
    return registry.confirm(outcome.confirmation_id)  # type: ignore[attr-defined]


def test_list_folder_is_bounded_sorted_and_non_recursive(tmp_path: Path) -> None:
    (tmp_path / "z-folder").mkdir()
    (tmp_path / "z-folder" / "hidden.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    tool = ListFolderTool(FilesystemPathValidator())

    result = tool.run(str(tmp_path))

    assert result.success
    assert "[Folder] z-folder" in result.message
    assert "[File] a.txt" in result.message
    assert "hidden.txt" not in result.message
    assert result.details["truncated"] is False


def test_list_folder_known_alias_needs_no_confirmation(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "manual.pdf").write_bytes(b"pdf")
    registry = make_registry(FakeLauncher(), home=tmp_path)

    result = registry.execute("list_folder", {"path": "Downloads"})

    assert isinstance(result, ToolResult) and result.success
    assert "manual.pdf" in result.message
    assert not registry.has_pending_confirmation()


@pytest.mark.parametrize("kind", ("missing", "file"))
def test_list_folder_rejects_non_directory(tmp_path: Path, kind: str) -> None:
    path = tmp_path / kind
    if kind == "file":
        path.write_text("x", encoding="utf-8")

    result = ListFolderTool(FilesystemPathValidator()).run(str(path))

    assert not result.success


def test_list_folder_truncates_at_one_hundred_entries(tmp_path: Path) -> None:
    for index in range(101):
        (tmp_path / f"file-{index:03}.txt").write_text("x", encoding="utf-8")

    result = ListFolderTool(FilesystemPathValidator()).run(str(tmp_path))

    assert result.success
    assert result.details["entry_count"] == 100
    assert result.details["truncated"] is True
    assert "truncated" in result.message


@pytest.mark.parametrize(
    ("entry_kind", "expected"),
    (("file", "file"), ("folder", "folder"), ("missing", "does not exist")),
)
def test_path_exists_reports_only_basic_type(
    tmp_path: Path,
    entry_kind: str,
    expected: str,
) -> None:
    path = tmp_path / entry_kind
    if entry_kind == "file":
        path.write_text("contents are never read", encoding="utf-8")
    elif entry_kind == "folder":
        path.mkdir()
    registry = make_registry(FakeLauncher())

    result = registry.execute("path_exists", {"path": str(path)})

    assert isinstance(result, ToolResult) and result.success
    assert expected in result.message
    assert not registry.has_pending_confirmation()


def test_create_folder_requires_confirmation_and_executes_exactly_once(tmp_path: Path) -> None:
    registry = make_registry(FakeLauncher())
    target = tmp_path / "Projects"

    outcome = registry.execute("create_folder", {"path": str(target)})

    assert isinstance(outcome, ConfirmationRequest)
    assert outcome.risk_level is RiskLevel.SENSITIVE
    assert outcome.summary == f"Create folder:\n{target.resolve(strict=False)}"
    assert not target.exists()
    first = registry.confirm(outcome.confirmation_id)
    second = registry.confirm(outcome.confirmation_id)
    assert first.success and target.is_dir()
    assert not second.success


def test_create_folder_cancel_and_invalid_destinations_change_nothing(tmp_path: Path) -> None:
    registry = make_registry(FakeLauncher())
    cancelled = tmp_path / "cancelled"
    request = registry.execute("create_folder", {"path": str(cancelled)})
    assert isinstance(request, ConfirmationRequest)

    registry.cancel(request.confirmation_id)
    existing = registry.execute("create_folder", {"path": str(tmp_path)})
    missing_parent = registry.execute("create_folder", {"path": str(tmp_path / "missing" / "child")})

    assert not cancelled.exists()
    assert isinstance(existing, ToolResult) and not existing.success
    assert isinstance(missing_parent, ToolResult) and not missing_parent.success


def test_create_folder_revalidates_destination_and_parent(tmp_path: Path) -> None:
    registry = make_registry(FakeLauncher())
    target = tmp_path / "appeared"
    request = registry.execute("create_folder", {"path": str(target)})
    assert isinstance(request, ConfirmationRequest)
    target.mkdir()

    result = registry.confirm(request.confirmation_id)

    assert not result.success
    assert target.is_dir()


def test_protected_location_policy_blocks_sensitive_mutation(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    policy = FilesystemSafetyPolicy((protected.resolve(),), ())
    validator = FilesystemPathValidator(policy)

    result = CreateFolderTool(validator).prepare(
        ToolArguments((("path", str(protected / "new")),))
    )

    assert isinstance(result, ToolResult) and not result.success
    assert "protected" in result.message.casefold()


@pytest.mark.parametrize(
    "value",
    (
        "   ",
        r"\\.\PhysicalDrive0",
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1",
        r"\\server\share\file.txt",
        r"C:relative.txt",
        r"C:\Users\name\CON.txt",
        r"C:\Users\name\bad?.txt",
        r"C:\Users\name\folder.\file.txt",
        r"C:\Users\name\..\Windows\file.txt",
    ),
)
def test_path_validator_rejects_dangerous_or_ambiguous_forms(value: str) -> None:
    with pytest.raises(FilesystemValidationError):
        FilesystemPathValidator().query_path(value)


def test_sensitive_mutation_rejects_ambiguous_relative_path(tmp_path: Path) -> None:
    relative_source = "draft.txt"
    absolute_destination = tmp_path / "final.txt"

    result = RenamePathTool(FilesystemPathValidator()).prepare(
        ToolArguments(
            (
                ("source", relative_source),
                ("destination", str(absolute_destination)),
            )
        )
    )

    assert isinstance(result, ToolResult) and not result.success
    assert "absolute path" in result.message


@pytest.mark.parametrize("entry_kind", ("file", "folder"))
def test_rename_file_or_folder_after_confirmation(tmp_path: Path, entry_kind: str) -> None:
    source = tmp_path / f"old-{entry_kind}"
    destination = tmp_path / f"new-{entry_kind}"
    source.mkdir() if entry_kind == "folder" else source.write_text("data", encoding="utf-8")
    registry = make_registry(FakeLauncher())

    request = registry.execute(
        "rename_path",
        {"source": str(source), "destination": str(destination)},
    )

    assert isinstance(request, ConfirmationRequest)
    assert request.summary == f"Rename:\n{source.resolve()}\n→\n{destination.resolve(strict=False)}"
    assert source.exists() and not destination.exists()
    assert confirm(registry, request).success
    assert not source.exists() and destination.exists()


def test_rename_cancel_missing_collision_same_and_other_parent_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "destination.txt"
    registry = make_registry(FakeLauncher())
    request = registry.execute("rename_path", {"source": str(source), "destination": str(destination)})
    assert isinstance(request, ConfirmationRequest)
    registry.cancel(request.confirmation_id)
    destination.write_text("destination", encoding="utf-8")

    missing = registry.execute("rename_path", {"source": str(tmp_path / "missing"), "destination": str(tmp_path / "new")})
    collision = registry.execute("rename_path", {"source": str(source), "destination": str(destination)})
    same = registry.execute("rename_path", {"source": str(source), "destination": str(source)})
    other = tmp_path / "other"
    other.mkdir()
    wrong_parent = registry.execute("rename_path", {"source": str(source), "destination": str(other / "source.txt")})

    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"
    assert all(isinstance(value, ToolResult) and not value.success for value in (missing, collision, same, wrong_parent))


@pytest.mark.parametrize("change", ("remove_source", "create_destination", "replace_source"))
def test_rename_final_revalidation_prevents_changed_action(tmp_path: Path, change: str) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("original", encoding="utf-8")
    registry = make_registry(FakeLauncher())
    request = registry.execute("rename_path", {"source": str(source), "destination": str(destination)})
    assert isinstance(request, ConfirmationRequest)
    if change == "remove_source":
        source.unlink()
    elif change == "create_destination":
        destination.write_text("do not overwrite", encoding="utf-8")
    else:
        source.unlink()
        source.write_text("replacement", encoding="utf-8")

    result = registry.confirm(request.confirmation_id)

    assert not result.success
    if destination.exists():
        assert destination.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.parametrize("entry_kind", ("file", "folder"))
def test_move_file_or_folder_after_confirmation(tmp_path: Path, entry_kind: str) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / entry_kind
    destination = destination_dir / entry_kind
    source.mkdir() if entry_kind == "folder" else source.write_text("data", encoding="utf-8")
    registry = make_registry(FakeLauncher())

    request = registry.execute("move_path", {"source": str(source), "destination": str(destination)})

    assert isinstance(request, ConfirmationRequest)
    assert source.exists() and not destination.exists()
    assert confirm(registry, request).success
    assert not source.exists() and destination.exists()


def test_move_cancel_and_invalid_destinations_never_merge_or_overwrite(tmp_path: Path) -> None:
    source_dir = tmp_path / "from"
    destination_dir = tmp_path / "to"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "item"
    destination = destination_dir / "item"
    source.mkdir()
    registry = make_registry(FakeLauncher())
    request = registry.execute("move_path", {"source": str(source), "destination": str(destination)})
    assert isinstance(request, ConfirmationRequest)
    registry.cancel(request.confirmation_id)
    destination.mkdir()

    collision = registry.execute("move_path", {"source": str(source), "destination": str(destination)})
    missing_parent = registry.execute("move_path", {"source": str(source), "destination": str(tmp_path / "missing" / "item")})
    missing_source = registry.execute("move_path", {"source": str(tmp_path / "none"), "destination": str(destination_dir / "none")})

    assert source.is_dir() and destination.is_dir()
    assert all(isinstance(value, ToolResult) and not value.success for value in (collision, missing_parent, missing_source))


def test_move_revalidates_source_destination_and_parent(tmp_path: Path) -> None:
    source_dir = tmp_path / "from"
    destination_dir = tmp_path / "to"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "file.txt"
    destination = destination_dir / "file.txt"
    source.write_text("source", encoding="utf-8")
    registry = make_registry(FakeLauncher())
    request = registry.execute("move_path", {"source": str(source), "destination": str(destination)})
    assert isinstance(request, ConfirmationRequest)
    destination.write_text("collision", encoding="utf-8")

    result = registry.confirm(request.confirmation_id)

    assert not result.success
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "collision"


def test_move_rejects_cross_volume_policy_failure(tmp_path: Path) -> None:
    class RejectVolumeValidator(FilesystemPathValidator):
        def assert_same_volume(self, source: Path, destination_parent: Path) -> None:
            raise FilesystemValidationError("Cross-volume moves are not supported.")

    source_dir = tmp_path / "from"
    destination_dir = tmp_path / "to"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "file.txt"
    source.write_text("x", encoding="utf-8")
    tool = MovePathTool(RejectVolumeValidator())

    result = tool.prepare(
        ToolArguments((("source", str(source)), ("destination", str(destination_dir / "file.txt"))))
    )

    assert isinstance(result, ToolResult) and not result.success
    assert "Cross-volume" in result.message


def test_multi_argument_preparation_is_deeply_immutable_and_exact(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new.txt"
    source.write_text("x", encoding="utf-8")
    registry = make_registry(FakeLauncher())

    action = registry.prepare(
        "rename_path",
        {"source": f'"{source}"', "destination": f'"{destination}"'},
    )

    assert isinstance(action, PreparedAction)
    assert action.normalized_arguments.values == (
        ("source", str(source.resolve())),
        ("destination", str(destination.resolve(strict=False))),
    )
    assert isinstance(action.execution_value, PreparedTransfer)
    with pytest.raises(FrozenInstanceError):
        action.execution_value.source = destination  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        action.normalized_arguments.values += (("risk_level", "safe"),)


def test_protected_source_and_destination_are_rejected(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    normal = tmp_path / "normal"
    protected.mkdir()
    normal.mkdir()
    protected_file = protected / "system.txt"
    normal_file = normal / "user.txt"
    protected_file.write_text("x", encoding="utf-8")
    normal_file.write_text("x", encoding="utf-8")
    validator = FilesystemPathValidator(FilesystemSafetyPolicy((protected.resolve(),), ()))
    tool = MovePathTool(validator)

    source_result = tool.prepare(
        ToolArguments((("source", str(protected_file)), ("destination", str(normal / "system.txt"))))
    )
    destination_result = tool.prepare(
        ToolArguments((("source", str(normal_file)), ("destination", str(protected / "user.txt"))))
    )

    assert isinstance(source_result, ToolResult) and not source_result.success
    assert isinstance(destination_result, ToolResult) and not destination_result.success
