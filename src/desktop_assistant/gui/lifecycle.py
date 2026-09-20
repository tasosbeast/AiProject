from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from desktop_assistant.gui.hotkey import GlobalHotkeyController
from desktop_assistant.gui.main_window import MainWindow


logger = logging.getLogger(__name__)


class DesktopLifecycleController(QObject):
    """Owns tray, hotkey, and the distinction between hiding and true shutdown."""

    def __init__(
        self,
        application: QApplication,
        window: MainWindow,
        *,
        tray_enabled: bool,
        hotkey: GlobalHotkeyController | None = None,
        tray_available: bool | None = None,
    ) -> None:
        super().__init__(application)
        self._application = application
        self._window = window
        self._hotkey = hotkey
        self._started = False
        self._shutdown_complete = False
        available = (
            QSystemTrayIcon.isSystemTrayAvailable()
            if tray_available is None
            else tray_available
        )
        self._tray_enabled = tray_enabled and available
        self.tray_icon: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        if self._tray_enabled:
            self._build_tray()
        self._application.aboutToQuit.connect(self.shutdown)

    @property
    def tray_active(self) -> bool:
        return self._tray_enabled and self.tray_icon is not None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._window.set_hide_on_close(self.tray_active)
        self._application.setQuitOnLastWindowClosed(not self.tray_active)
        if self.tray_icon is not None:
            self.tray_icon.show()
            logger.info("System tray icon started")
        if self._hotkey is not None and not self._hotkey.register(self._window.show_and_focus):
            logger.warning("The configured global hotkey could not be registered")

    @Slot()
    def show_window(self) -> None:
        self._window.show_and_focus()

    @Slot()
    def hide_window(self) -> None:
        self._window.hide_to_tray()

    @Slot()
    def request_quit(self) -> None:
        if self._shutdown_complete:
            return
        self.shutdown()
        self._window.prepare_for_quit()
        self._window.close()
        self._application.quit()

    @Slot()
    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        if self._hotkey is not None:
            self._hotkey.unregister()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self._window.perform_shutdown()
        logger.info("Desktop assistant lifecycle stopped")

    def _build_tray(self) -> None:
        icon = self._application.windowIcon()
        if icon.isNull():
            icon = self._window.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self._application.setWindowIcon(icon)
            self._window.setWindowIcon(icon)
        tray = QSystemTrayIcon(QIcon(icon), self)
        tray.setToolTip("AI Assistant")
        menu = QMenu()
        show_action = QAction("Show Assistant", menu)
        hide_action = QAction("Hide Assistant", menu)
        quit_action = QAction("Quit", menu)
        show_action.triggered.connect(self.show_window)
        hide_action.triggered.connect(self.hide_window)
        quit_action.triggered.connect(self.request_quit)
        menu.addAction(show_action)
        menu.addAction(hide_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._handle_tray_activation)
        self.tray_icon = tray
        self.tray_menu = menu

    @Slot(QSystemTrayIcon.ActivationReason)
    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason is QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()
