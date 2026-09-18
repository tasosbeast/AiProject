from __future__ import annotations

import logging
from typing import Protocol

from PySide6.QtCore import QThreadPool, Qt, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop_assistant.gui.widgets import CommandInput, ConversationView, MessageKind
from desktop_assistant.gui.worker import AssistantWorker
from desktop_assistant.models import ToolResult


logger = logging.getLogger(__name__)


class AssistantHandler(Protocol):
    def handle(self, request: str) -> ToolResult: ...


class MainWindow(QMainWindow):
    def __init__(
        self,
        assistant: AssistantHandler,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__()
        self._assistant = assistant
        self._thread_pool = thread_pool or QThreadPool(self)
        self._thread_pool.setMaxThreadCount(1)
        self._active_worker: AssistantWorker | None = None
        self._processing = False

        self.setWindowTitle("AI Assistant")
        self.resize(810, 620)
        self.setMinimumSize(620, 480)
        self.setCentralWidget(self._build_ui())

        self.command_input.submitted.connect(self.submit_command)
        self.send_button.clicked.connect(self.submit_command)
        self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def is_processing(self) -> bool:
        return self._processing

    def _build_ui(self) -> QWidget:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())

        self.conversation = ConversationView()
        root_layout.addWidget(self.conversation, 1)

        input_region = QWidget()
        input_layout = QVBoxLayout(input_region)
        input_layout.setContentsMargins(24, 8, 24, 22)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(11, 9, 9, 9)
        composer_layout.setSpacing(10)

        self.command_input = CommandInput()
        self.command_input.setObjectName("commandInput")
        self.command_input.setPlaceholderText("Type a command...")
        self.command_input.setMinimumHeight(44)
        self.command_input.setMaximumHeight(112)
        self.command_input.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.MinimumExpanding,
        )

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("sendButton")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.setMinimumHeight(40)

        composer_layout.addWidget(self.command_input, 1)
        composer_layout.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        input_layout.addWidget(composer)
        root_layout.addWidget(input_region)
        return root

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(68)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(11)

        mark = QLabel("AI")
        mark.setObjectName("assistantMark")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("AI Assistant")
        title.setObjectName("assistantName")

        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(8, 8)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusText")

        layout.addWidget(mark)
        layout.addWidget(title)
        layout.addStretch(1)
        layout.addWidget(self.status_dot)
        layout.addWidget(self.status_label)
        return header

    @Slot()
    def submit_command(self) -> None:
        if self._processing:
            return

        command = self.command_input.toPlainText().strip()
        if not command:
            self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return

        self.conversation.add_message(MessageKind.USER, command)
        self.command_input.clear()
        self._set_processing(True)

        worker = AssistantWorker(self._assistant.handle, command)
        worker.signals.succeeded.connect(self._handle_result)
        worker.signals.failed.connect(self._handle_failure)
        self._active_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _handle_result(self, result: object) -> None:
        if not isinstance(result, ToolResult):
            logger.error("Assistant returned an unexpected result type: %s", type(result).__name__)
            self._handle_failure()
            return

        kind = MessageKind.ASSISTANT if result.success else MessageKind.ERROR
        self.conversation.add_message(kind, result.message)
        self._finish_processing()

    @Slot()
    def _handle_failure(self) -> None:
        self.conversation.add_message(
            MessageKind.ERROR,
            "Something went wrong while processing that command. Please try again.",
        )
        self._finish_processing()

    def _finish_processing(self) -> None:
        self._active_worker = None
        self._set_processing(False)
        self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_processing(self, processing: bool) -> None:
        self._processing = processing
        self.send_button.setEnabled(not processing)
        self.command_input.setEnabled(not processing)
        self.status_label.setText("Working..." if processing else "Ready")
        self.status_dot.setProperty("state", "working" if processing else "ready")
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
