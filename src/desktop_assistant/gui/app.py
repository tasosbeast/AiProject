from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from desktop_assistant.bootstrap import build_assistant, build_voice_services
from desktop_assistant.config import load_settings
from desktop_assistant.gui.audio import QtSpeechPlayer, QtVoiceRecorder
from desktop_assistant.gui.hotkey import WindowsGlobalHotkeyController
from desktop_assistant.gui.lifecycle import DesktopLifecycleController
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
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = create_application()
    voice_services = build_voice_services(settings)
    recorder = QtVoiceRecorder(settings.maximum_recording_seconds)
    player = QtSpeechPlayer() if voice_services.speech is not None else None
    window = MainWindow(
        build_assistant(settings=settings),
        recorder=recorder,
        transcription_provider=voice_services.transcription,
        speech_provider=voice_services.speech,
        speech_player=player,
        voice_output_enabled=settings.voice_output_enabled,
    )
    hotkey = None
    try:
        hotkey = WindowsGlobalHotkeyController(application, settings.global_hotkey)
    except ValueError:
        logging.getLogger(__name__).warning("Invalid GLOBAL_HOTKEY configuration; hotkey disabled")
    lifecycle = DesktopLifecycleController(
        application,
        window,
        tray_enabled=settings.system_tray_enabled,
        hotkey=hotkey,
    )
    lifecycle.start()
    window.show()
    return application.exec()
