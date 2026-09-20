from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from desktop_assistant.config import load_settings
from desktop_assistant.logging_setup import (
    LOG_BACKUP_COUNT,
    MAX_LOG_BYTES,
    configure_logging,
)
from desktop_assistant.runtime_paths import RuntimePaths


def test_source_runtime_paths_are_module_relative_not_cwd(tmp_path, monkeypatch) -> None:
    repository = tmp_path / "repository"
    module = repository / "src" / "desktop_assistant" / "runtime_paths.py"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    paths = RuntimePaths.detect(frozen=False, module_file=module)

    assert paths.application_root == repository.resolve()
    assert paths.env_file == repository.resolve() / ".env.local"
    assert paths.env_file.parent != unrelated


def test_frozen_paths_use_local_app_data_not_executable_or_cwd(tmp_path, monkeypatch) -> None:
    local_data = tmp_path / "LocalAppData"
    install = tmp_path / "relocated" / "bundle"
    unrelated = tmp_path / "cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    paths = RuntimePaths.detect(
        frozen=True,
        executable=install / "AiAssistant.exe",
        local_app_data=local_data,
    )

    expected = local_data.resolve() / "AiProject" / "AI Assistant"
    assert paths.application_root == install.resolve()
    assert paths.config_directory == expected
    assert paths.env_file == expected / ".env.local"
    assert paths.log_directory == expected / "logs"
    assert unrelated not in paths.env_file.parents


def test_frozen_configuration_survives_bundle_relocation(tmp_path) -> None:
    local_data = tmp_path / "LocalAppData"
    first = RuntimePaths.detect(
        frozen=True,
        executable=tmp_path / "one" / "AiAssistant.exe",
        local_app_data=local_data,
    )
    second = RuntimePaths.detect(
        frozen=True,
        executable=tmp_path / "two" / "AiAssistant.exe",
        local_app_data=local_data,
    )

    assert first.application_root != second.application_root
    assert first.env_file == second.env_file
    assert first.log_directory == second.log_directory


def test_load_settings_uses_runtime_env_and_preserves_os_precedence(tmp_path, monkeypatch) -> None:
    paths = RuntimePaths.detect(
        frozen=True,
        executable=tmp_path / "app" / "AiAssistant.exe",
        local_app_data=tmp_path / "data",
    )
    paths.config_directory.mkdir(parents=True)
    paths.env_file.write_text(
        "OPENAI_MODEL=dotenv-model\nASSISTANT_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_MODEL", "process-model")
    monkeypatch.delenv("ASSISTANT_LOG_LEVEL", raising=False)

    settings = load_settings(runtime_paths=paths)

    assert settings.openai_model == "process-model"
    assert settings.log_level == "DEBUG"


def test_frozen_logging_is_bounded_and_written_under_local_app_data(tmp_path) -> None:
    paths = RuntimePaths.detect(
        frozen=True,
        executable=tmp_path / "app" / "AiAssistant.exe",
        local_app_data=tmp_path / "data",
    )
    log_path = configure_logging("INFO", paths)

    assert log_path == paths.log_directory / "assistant.log"
    handler = next(
        item for item in logging.getLogger().handlers if isinstance(item, RotatingFileHandler)
    )
    assert handler.maxBytes == MAX_LOG_BYTES
    assert handler.backupCount == LOG_BACKUP_COUNT
    logging.getLogger(__name__).info("packaged logging test")
    handler.flush()
    assert log_path is not None and "packaged logging test" in log_path.read_text("utf-8")
    handler.close()
    logging.getLogger().handlers.clear()


def test_source_logging_tolerates_missing_stderr(tmp_path, monkeypatch) -> None:
    paths = RuntimePaths.detect(
        frozen=False,
        module_file=tmp_path / "repo" / "src" / "desktop_assistant" / "runtime_paths.py",
    )
    monkeypatch.setattr("sys.stderr", None)

    assert configure_logging("INFO", paths) is None
    logging.getLogger(__name__).info("must not fail")
    logging.getLogger().handlers.clear()


def test_frozen_app_ignores_cwd_env_file_regression(tmp_path, monkeypatch) -> None:
    malicious_cwd = tmp_path / "RandomFolder"
    malicious_cwd.mkdir()
    (malicious_cwd / ".env.local").write_text(
        "OPENAI_MODEL=malicious-model\nASSISTANT_LOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(malicious_cwd)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("ASSISTANT_LOG_LEVEL", raising=False)

    local_data = tmp_path / "LocalAppData"
    paths = RuntimePaths.detect(
        frozen=True,
        executable=tmp_path / "app" / "AiAssistant.exe",
        local_app_data=local_data,
    )
    settings = load_settings(runtime_paths=paths)

    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.log_level == "INFO"

