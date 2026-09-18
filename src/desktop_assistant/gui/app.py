from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from desktop_assistant.bootstrap import build_assistant
from desktop_assistant.config import Settings
from desktop_assistant.gui.main_window import MainWindow
from desktop_assistant.gui.styles import DARK_STYLESHEET


def create_application(argv: list[str] | None = None) -> QApplication:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(argv if argv is not None else sys.argv)
    application.setApplicationName("AI Assistant")
    application.setOrganizationName("AiProject")
    application.setStyle("Fusion")
    application.setStyleSheet(DARK_STYLESHEET)
    return application


def main() -> int:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = create_application()
    window = MainWindow(build_assistant())
    window.show()
    return application.exec()
