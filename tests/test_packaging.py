from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_packaging_spec_is_windowed_onedir_and_has_dynamic_imports() -> None:
    source = (ROOT / "packaging" / "AiAssistant.spec").read_text(encoding="utf-8")

    assert 'name="AiAssistant"' in source
    assert "console=False" in source
    assert "COLLECT(" in source
    assert "desktop_assistant.intent.openai_provider" in source
    assert "desktop_assistant.voice.openai_audio" in source
    assert "PySide6.QtMultimedia" in source
    assert "PySide6.QtNetwork" in source
    assert "msvc_runtime_binaries" in source
    assert "ambient_dlls" in source
    assert "C:\\Users" not in source


def test_build_script_is_location_independent_and_scans_secrets() -> None:
    source = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    assert "$PSScriptRoot" in source
    assert "AiAssistant.spec" in source
    assert "AiAssistant.exe" in source
    assert ".env.local" in source
    assert "sk-proj-" in source


def test_packaging_dependencies_and_outputs_are_declared_safely() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert 'pyinstaller>=6.22,<7.0' in project
    assert "build/" in ignored
    assert "dist/" in ignored
    assert ".env.local" in ignored


def test_ci_builds_on_windows_without_secret_configuration() -> None:
    source = (ROOT / ".github" / "workflows" / "windows-package.yml").read_text(
        encoding="utf-8"
    )

    assert "windows-latest" in source
    assert "pytest" in source
    assert "compileall" in source
    assert "build_windows.ps1 -Clean" in source
    assert "OPENAI_API_KEY" not in source
