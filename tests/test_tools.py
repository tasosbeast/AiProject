from __future__ import annotations

from desktop_assistant.config import AppCatalog
from desktop_assistant.models import RiskLevel
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool

from conftest import FakeLauncher


def test_open_app_uses_allowlist() -> None:
    launcher = FakeLauncher()
    tool = OpenAppTool(launcher, AppCatalog())

    result = tool.run("spotify")

    assert result.success
    assert result.risk_level is RiskLevel.SAFE
    assert launcher.apps[0].display_name == "Spotify"


def test_open_app_rejects_unknown_executable() -> None:
    launcher = FakeLauncher()
    result = OpenAppTool(launcher, AppCatalog()).run("malware.exe --flag")

    assert not result.success
    assert launcher.apps == []


def test_open_folder_requires_existing_directory(tmp_path) -> None:
    launcher = FakeLauncher()
    tool = OpenFolderTool(launcher)

    good = tool.run(str(tmp_path))
    missing = tool.run(str(tmp_path / "missing"))

    assert good.success
    assert launcher.folders == [tmp_path.resolve()]
    assert not missing.success


def test_open_folder_rejects_file(tmp_path) -> None:
    launcher = FakeLauncher()
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello", encoding="utf-8")

    result = OpenFolderTool(launcher).run(str(file_path))

    assert not result.success
    assert launcher.folders == []


def test_open_website_accepts_http_and_https() -> None:
    launcher = FakeLauncher()
    tool = OpenWebsiteTool(launcher)

    assert tool.run("https://example.com/docs?q=1").success
    assert tool.run("http://localhost:8000").success
    assert launcher.websites == ["https://example.com/docs?q=1", "http://localhost:8000"]


def test_open_website_rejects_unsafe_or_malformed_urls() -> None:
    launcher = FakeLauncher()
    tool = OpenWebsiteTool(launcher)

    rejected = (
        "file:///C:/Windows/System32",
        "javascript:alert(1)",
        "https://user:password@example.com",
        "https://example.com/bad path",
        "https://example.com:99999",
    )

    assert all(not tool.run(url).success for url in rejected)
    assert launcher.websites == []

