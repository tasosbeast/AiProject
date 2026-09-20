from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_assistant.bootstrap import build_assistant, build_voice_services
from desktop_assistant.config import load_settings
from desktop_assistant.gui.audio import QtSpeechPlayer, QtVoiceRecorder
from desktop_assistant.gui.hotkey import WindowsGlobalHotkeyController
from desktop_assistant.gui.lifecycle import DesktopLifecycleController
from desktop_assistant.gui.main_window import MainWindow
from desktop_assistant.gui.single_instance import SingleInstanceCoordinator
from desktop_assistant.gui.styles import DARK_STYLESHEET
from desktop_assistant.logging_setup import configure_logging
from desktop_assistant.runtime_paths import RuntimePaths


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
    application: QApplication | None = None
    instance: SingleInstanceCoordinator | None = None
    try:
        paths = RuntimePaths.detect()
        configure_logging("INFO", paths)
        application = create_application()
        instance = SingleInstanceCoordinator(application)
        if not instance.acquire():
            return 0

        settings = load_settings(runtime_paths=paths)
        configure_logging(settings.log_level, paths)
        logging.getLogger(__name__).info(
            "Desktop assistant starting (%s mode)",
            "packaged" if paths.is_frozen else "source",
        )
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
        instance.set_activation_callback(window.show_and_focus)
        application.aboutToQuit.connect(instance.shutdown)
        hotkey = None
        try:
            hotkey = WindowsGlobalHotkeyController(application, settings.global_hotkey)
        except ValueError:
            logging.getLogger(__name__).warning(
                "Invalid GLOBAL_HOTKEY configuration; hotkey disabled"
            )
        lifecycle = DesktopLifecycleController(
            application,
            window,
            tray_enabled=settings.system_tray_enabled,
            hotkey=hotkey,
        )
        lifecycle.start()
        window.show()
        return application.exec()
    except Exception:
        logging.getLogger(__name__).exception("Desktop assistant failed during startup")
        if instance is not None:
            instance.shutdown()
        if application is None:
            application = create_application([])
        QMessageBox.critical(
            None,
            "AI Assistant",
            "AI Assistant could not start. Check the application log for details.",
        )
        return 1
