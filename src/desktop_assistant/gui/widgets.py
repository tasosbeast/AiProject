from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop_assistant.models import ConfirmationRequest, RiskLevel


class MessageKind(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    ERROR = "error"


class CommandInput(QPlainTextEdit):
    """Multiline command input where Enter submits and Shift+Enter adds a line."""

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            event.accept()
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class MessageBubble(QFrame):
    def __init__(self, kind: MessageKind, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.text = text
        self.setProperty("messageKind", kind.value)
        self.setMaximumWidth(590)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 11, 15, 13)
        layout.setSpacing(5)

        role = QLabel("You" if kind is MessageKind.USER else "AI Assistant")
        role.setObjectName("messageRole")

        body = QLabel(text)
        body.setObjectName("messageText")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setWordWrap(True)

        layout.addWidget(role)
        layout.addWidget(body)


class ConfirmationCard(QFrame):
    """Inline, non-editable presentation of one core-owned confirmation request."""

    confirmed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, request: ConfirmationRequest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.request = request
        self.setObjectName("confirmationCard")
        self.setProperty("risk", request.risk_level.value)
        self.setMaximumWidth(590)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(17, 15, 17, 15)
        layout.setSpacing(9)

        heading = QLabel(
            "Confirm destructive action"
            if request.risk_level is RiskLevel.DESTRUCTIVE
            else "Allow this action?"
        )
        heading.setObjectName("confirmationTitle")
        summary = QLabel(request.summary)
        summary.setObjectName("confirmationSummary")
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary.setWordWrap(True)
        risk = QLabel(f"Risk: {request.risk_level.value.title()}")
        risk.setObjectName("confirmationRisk")
        warning = QLabel(request.warning)
        warning.setObjectName("confirmationWarning")
        warning.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("confirmationCancel")
        confirm = QPushButton("Confirm")
        confirm.setObjectName("confirmationConfirm")
        confirm.setProperty("risk", request.risk_level.value)
        cancel.clicked.connect(lambda: self.cancelled.emit(request.confirmation_id))
        confirm.clicked.connect(lambda: self.confirmed.emit(request.confirmation_id))
        controls.addWidget(cancel)
        controls.addWidget(confirm)

        layout.addWidget(heading)
        layout.addWidget(summary)
        layout.addWidget(risk)
        if request.warning:
            layout.addWidget(warning)
        layout.addLayout(controls)

        self.confirm_button = confirm
        self.cancel_button = cancel


class ConversationView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("conversationScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(28, 28, 28, 24)
        self._layout.setSpacing(14)
        self.setWidget(self._content)

        self.messages: list[MessageBubble] = []
        self.confirmations: list[ConfirmationCard] = []
        self._welcome = self._create_welcome()
        self._layout.addWidget(self._welcome)
        self._layout.addStretch(1)

    @staticmethod
    def _create_welcome() -> QFrame:
        card = QFrame()
        card.setObjectName("welcomeCard")
        card.setMaximumWidth(540)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(9)

        title = QLabel("AI Assistant")
        title.setObjectName("welcomeTitle")
        description = QLabel("Ready to help with your computer.")
        description.setObjectName("welcomeText")
        examples = QLabel(
            "Try:\n"
            "  open spotify\n"
            "  open chrome\n"
            "  open website https://github.com"
        )
        examples.setObjectName("exampleText")

        layout.addWidget(title)
        layout.addWidget(description)
        layout.addSpacing(5)
        layout.addWidget(examples)
        return card

    def add_message(self, kind: MessageKind, text: str) -> MessageBubble:
        if not self.messages:
            self._welcome.hide()

        bubble = MessageBubble(kind, text)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if kind is MessageKind.USER:
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)

        insert_at = self._layout.count() - 1
        self._layout.insertLayout(insert_at, row)
        self.messages.append(bubble)
        QTimer.singleShot(0, self.scroll_to_latest)
        return bubble

    def add_confirmation(self, request: ConfirmationRequest) -> ConfirmationCard:
        if not self.messages:
            self._welcome.hide()
        card = ConfirmationCard(request)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(card)
        row.addStretch(1)
        self._layout.insertLayout(self._layout.count() - 1, row)
        self.confirmations.append(card)
        QTimer.singleShot(0, self.scroll_to_latest)
        return card

    def scroll_to_latest(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
