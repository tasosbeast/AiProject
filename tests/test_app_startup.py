from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from desktop_assistant.gui import app as app_module


class FakeInstance:
    should_acquire = False
    latest: "FakeInstance | None" = None

    def __init__(self, _parent: object) -> None:
        self.shutdown_calls = 0
        type(self).latest = self

    def acquire(self) -> bool:
        return self.should_acquire

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_second_instance_exits_before_building_assistant_or_voice(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    FakeInstance.should_acquire = False
    monkeypatch.setattr(app_module, "create_application", lambda: qt_app)
    monkeypatch.setattr(app_module, "SingleInstanceCoordinator", FakeInstance)
    monkeypatch.setattr(app_module, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(
        app_module,
        "build_assistant",
        lambda **_kwargs: pytest.fail("secondary instance built the assistant"),
    )
    monkeypatch.setattr(
        app_module,
        "build_voice_services",
        lambda _settings: pytest.fail("secondary instance built voice providers"),
    )

    assert app_module.main() == 0


def test_startup_failure_is_logged_and_shown_without_console(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    FakeInstance.should_acquire = True
    messages: list[str] = []
    monkeypatch.setattr(app_module, "create_application", lambda: qt_app)
    monkeypatch.setattr(app_module, "SingleInstanceCoordinator", FakeInstance)
    monkeypatch.setattr(app_module, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private startup detail")),
    )
    monkeypatch.setattr(
        app_module.QMessageBox,
        "critical",
        lambda _parent, _title, message: messages.append(message),
    )

    assert app_module.main() == 1
    assert FakeInstance.latest is not None
    assert FakeInstance.latest.shutdown_calls == 1
    assert messages == ["AI Assistant could not start. Check the application log for details."]
    assert "private startup detail" not in messages[0]
