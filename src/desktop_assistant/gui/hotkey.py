from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HotkeyDefinition:
    modifiers: int
    virtual_key: int
    display_text: str


class GlobalHotkeyController(Protocol):
    def register(self, callback: Callable[[], None]) -> bool: ...

    def unregister(self) -> None: ...


_MODIFIERS = {
    "alt": (0x0001, "Alt"),
    "ctrl": (0x0002, "Ctrl"),
    "control": (0x0002, "Ctrl"),
    "shift": (0x0004, "Shift"),
    "win": (0x0008, "Win"),
    "windows": (0x0008, "Win"),
}
_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_NAMED_KEYS = {"space": (0x20, "Space")}


def parse_hotkey(value: str) -> HotkeyDefinition:
    parts = tuple(part.strip() for part in value.split("+") if part.strip())
    if len(parts) < 2:
        raise ValueError("A global hotkey requires a modifier and one key.")

    modifier_value = 0
    modifier_names: set[str] = set()
    key_value: int | None = None
    key_name: str | None = None
    for part in parts:
        normalized = part.casefold()
        modifier = _MODIFIERS.get(normalized)
        if modifier is not None:
            bit, display_name = modifier
            if modifier_value & bit:
                raise ValueError("A global hotkey cannot repeat modifiers.")
            modifier_value |= bit
            modifier_names.add(display_name)
            continue
        if key_value is not None:
            raise ValueError("A global hotkey must contain exactly one non-modifier key.")
        named = _NAMED_KEYS.get(normalized)
        if named is not None:
            key_value, key_name = named
        elif len(part) == 1 and part.isascii() and part.isalnum():
            key_name = part.upper()
            key_value = ord(key_name)
        elif normalized.startswith("f") and normalized[1:].isdigit():
            number = int(normalized[1:])
            if not 1 <= number <= 24:
                raise ValueError("Function-key hotkeys must use F1 through F24.")
            key_name = f"F{number}"
            key_value = 0x70 + number - 1
        else:
            raise ValueError("The configured global hotkey is not supported.")

    if not modifier_value or key_value is None or key_name is None:
        raise ValueError("A global hotkey requires a modifier and one key.")
    ordered_modifiers = [name for name in _MODIFIER_ORDER if name in modifier_names]
    return HotkeyDefinition(
        modifier_value,
        key_value,
        "+".join((*ordered_modifiers, key_name)),
    )


class WindowsGlobalHotkeyController(QAbstractNativeEventFilter):
    """Registers one Win32 hotkey and forwards WM_HOTKEY on Qt's UI thread."""

    _WM_HOTKEY = 0x0312
    _MOD_NOREPEAT = 0x4000
    _HOTKEY_ID = 0xA11A

    def __init__(self, application: QCoreApplication, shortcut: str) -> None:
        super().__init__()
        self._application = application
        self._definition = parse_hotkey(shortcut)
        self._callback: Callable[[], None] | None = None
        self._registration_attempted = False
        self._registered = False
        self._user32: object | None = None
        self._message_type: type[ctypes.Structure] | None = None

    @property
    def display_text(self) -> str:
        return self._definition.display_text

    def register(self, callback: Callable[[], None]) -> bool:
        if self._registration_attempted:
            return self._registered
        self._registration_attempted = True
        self._callback = callback
        if os.name != "nt":
            logger.warning("Global hotkey is unavailable outside Windows")
            return False

        from ctypes import wintypes

        class Message(ctypes.Structure):
            _fields_ = (
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            )

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL
        self._message_type = Message
        self._user32 = user32
        self._application.installNativeEventFilter(self)
        registered = bool(
            user32.RegisterHotKey(
                None,
                self._HOTKEY_ID,
                self._definition.modifiers | self._MOD_NOREPEAT,
                self._definition.virtual_key,
            )
        )
        if not registered:
            self._application.removeNativeEventFilter(self)
            logger.warning("Global hotkey registration failed: %s", self.display_text)
            return False
        self._registered = True
        logger.info("Global hotkey registered: %s", self.display_text)
        return True

    def unregister(self) -> None:
        if not self._registered:
            return
        user32 = self._user32
        self._registered = False
        self._application.removeNativeEventFilter(self)
        if user32 is not None:
            user32.UnregisterHotKey(None, self._HOTKEY_ID)  # type: ignore[attr-defined]
        logger.info("Global hotkey unregistered: %s", self.display_text)

    def nativeEventFilter(self, event_type: bytes, message: int) -> tuple[bool, int]:  # noqa: N802
        if not self._registered or self._message_type is None:
            return False, 0
        native_event_type = bytes(event_type)
        if native_event_type not in {b"windows_generic_MSG", b"windows_dispatcher_MSG"}:
            return False, 0
        native_message = self._message_type.from_address(int(message))
        if (
            native_message.message == self._WM_HOTKEY
            and native_message.wParam == self._HOTKEY_ID
        ):
            if self._callback is not None:
                self._callback()
            return True, 0
        return False, 0
