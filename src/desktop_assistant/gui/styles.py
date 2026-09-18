from __future__ import annotations


DARK_STYLESHEET = """
QWidget {
    background-color: #0c1118;
    color: #e7ecf3;
    font-family: "Segoe UI";
    font-size: 14px;
}

QLabel {
    background-color: transparent;
}

QMainWindow {
    background-color: #0c1118;
}

QFrame#header {
    background-color: #111822;
    border-bottom: 1px solid #263140;
}

QLabel#assistantMark {
    background-color: #5b8cff;
    color: #ffffff;
    border-radius: 15px;
    font-size: 15px;
    font-weight: 700;
}

QLabel#assistantName {
    color: #f5f7fb;
    font-size: 17px;
    font-weight: 650;
}

QLabel#statusDot {
    background-color: #55c48b;
    border-radius: 4px;
}

QLabel#statusDot[state="working"] {
    background-color: #e6b85c;
}

QLabel#statusText {
    color: #96a3b4;
    font-size: 12px;
}

QScrollArea#conversationScroll,
QScrollArea#conversationScroll > QWidget > QWidget {
    background-color: #0c1118;
    border: none;
}

QFrame#welcomeCard {
    background-color: #111822;
    border: 1px solid #263140;
    border-radius: 14px;
}

QLabel#welcomeTitle {
    color: #f5f7fb;
    font-size: 21px;
    font-weight: 650;
}

QLabel#welcomeText {
    color: #aab4c2;
    font-size: 14px;
}

QLabel#exampleText {
    color: #7f8da0;
    font-family: "Cascadia Mono", "Consolas";
    font-size: 12px;
}

QFrame[messageKind="assistant"] {
    background-color: #151d28;
    border: 1px solid #263140;
    border-radius: 13px;
}

QFrame[messageKind="user"] {
    background-color: #294f8f;
    border: 1px solid #3963a5;
    border-radius: 13px;
}

QFrame[messageKind="error"] {
    background-color: #241a20;
    border: 1px solid #71404c;
    border-radius: 13px;
}

QLabel#messageRole {
    color: #93a1b3;
    font-size: 11px;
    font-weight: 650;
}

QFrame[messageKind="user"] QLabel#messageRole {
    color: #dce8fb;
}

QLabel#messageText {
    color: #edf1f7;
    font-size: 14px;
}

QFrame#composer {
    background-color: #111822;
    border: 1px solid #2a3544;
    border-radius: 14px;
}

QPlainTextEdit#commandInput {
    background-color: transparent;
    color: #eef2f7;
    border: none;
    padding: 5px;
    selection-background-color: #3d69a8;
}

QPlainTextEdit#commandInput:disabled {
    color: #697587;
}

QPushButton#sendButton {
    background-color: #5b8cff;
    color: #ffffff;
    border: none;
    border-radius: 10px;
    padding: 9px 18px;
    font-weight: 650;
}

QPushButton#sendButton:hover {
    background-color: #70a0ff;
}

QPushButton#sendButton:pressed {
    background-color: #4d79d8;
}

QPushButton#sendButton:disabled {
    background-color: #2a3544;
    color: #758194;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 3px;
}

QScrollBar::handle:vertical {
    background: #394556;
    border-radius: 4px;
    min-height: 28px;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}
"""
