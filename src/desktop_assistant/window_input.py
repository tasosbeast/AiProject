from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from enum import Enum
import logging
import os
from types import MappingProxyType
from typing import Callable, Protocol

from desktop_assistant.config import AppCatalog
from desktop_assistant.editable_controls import (
    EditableControlError, EditableControlResolver, WindowsEditableControlResolver,
)
from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult
from desktop_assistant.process_control import WindowController, WindowInfo
from desktop_assistant.windows import bounded_window_label, match_window_for_focus


logger = logging.getLogger(__name__)
MAX_TEXT_LENGTH = 2000
TEXT_PREVIEW_LENGTH = 160


class InputAction(str, Enum):
    TYPE_TEXT = "type_text"
    TYPE_TEXT_AND_ENTER = "type_text_and_enter"
    ENTER = "enter"
    ESCAPE = "escape"
    TAB = "tab"
    BACKSPACE = "backspace"
    DELETE = "delete"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ARROW_LEFT = "arrow_left"
    ARROW_RIGHT = "arrow_right"
    HOME = "home"
    END = "end"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    CTRL_A = "ctrl_a"
    CTRL_C = "ctrl_c"
    CTRL_V = "ctrl_v"
    CTRL_S = "ctrl_s"
    CTRL_F = "ctrl_f"
    CTRL_L = "ctrl_l"
    CTRL_Z = "ctrl_z"
    CTRL_Y = "ctrl_y"


_TEXT_ACTIONS = frozenset({InputAction.TYPE_TEXT, InputAction.TYPE_TEXT_AND_ENTER})
_KEYUP = 0x0002
_UNICODE = 0x0004
_EXTENDED = 0x0001
# Only application-owned virtual keys. The public tool cannot supply these codes.
_KEYS = MappingProxyType({
    InputAction.ENTER: (0x0D,), InputAction.ESCAPE: (0x1B,),
    InputAction.TAB: (0x09,), InputAction.BACKSPACE: (0x08,),
    InputAction.DELETE: (0x2E,), InputAction.ARROW_UP: (0x26,),
    InputAction.ARROW_DOWN: (0x28,), InputAction.ARROW_LEFT: (0x25,),
    InputAction.ARROW_RIGHT: (0x27,), InputAction.HOME: (0x24,),
    InputAction.END: (0x23,), InputAction.PAGE_UP: (0x21,),
    InputAction.PAGE_DOWN: (0x22,), InputAction.CTRL_A: (0x11, 0x41),
    InputAction.CTRL_C: (0x11, 0x43), InputAction.CTRL_V: (0x11, 0x56),
    InputAction.CTRL_S: (0x11, 0x53), InputAction.CTRL_F: (0x11, 0x46),
    InputAction.CTRL_L: (0x11, 0x4C), InputAction.CTRL_Z: (0x11, 0x5A),
    InputAction.CTRL_Y: (0x11, 0x59),
})
_EXTENDED_KEYS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E})


def _validate_input(action: InputAction, value: str | None) -> None:
    if not isinstance(action, InputAction):
        raise ValueError("Unsupported input action.")
    if action in _TEXT_ACTIONS:
        if not isinstance(value, str) or not value:
            raise ValueError("Text actions require non-empty text.")
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"Text must contain at most {MAX_TEXT_LENGTH} characters.")
        try:
            value.encode("utf-16-le")
        except UnicodeEncodeError:
            raise ValueError("Text must contain valid Unicode characters.") from None
    elif value is not None:
        raise ValueError("Only text actions accept a value.")


@dataclass(frozen=True, slots=True)
class PreparedWindowInput:
    handle: int
    process_id: int
    title: str
    executable_name: str
    action: InputAction
    value: str | None = field(default=None, repr=False)


class InputController(Protocol):
    def send(
        self, action: InputAction, value: str | None, verify_target: Callable[[], bool],
    ) -> None: ...


class InputError(RuntimeError):
    """A keyboard request was refused or could not be fully delivered."""


@dataclass(frozen=True, slots=True)
class _KeyEvent:
    vk: int = 0
    scan: int = 0
    flags: int = 0

    def release(self) -> _KeyEvent:
        return _KeyEvent(self.vk, self.scan, self.flags | _KEYUP)


# Fixed-width Win32 types keep the ABI correct on both 32- and 64-bit Windows.
class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    # INPUT must reserve the full native union size (including its largest member).
    # This is ABI padding only: this backend creates keyboard events exclusively.
    _fields_ = [("ki", _KeyboardInput),
                ("_padding", ctypes.c_byte * (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24))]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("data", _InputUnion)]


class WindowsInputController:
    """One bounded SendInput batch; an injected callable keeps tests off the desktop."""

    def __init__(self, send_input: Callable[..., int] | None = None) -> None:
        self._send_input = send_input

    def _sender(self) -> Callable[..., int]:
        if self._send_input is None:
            if os.name != "nt":
                raise InputError("Keyboard input is only available on Windows.")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            sender = user32.SendInput
            sender.argtypes = (ctypes.c_uint32, ctypes.POINTER(_Input), ctypes.c_int)
            sender.restype = ctypes.c_uint32
            self._send_input = sender
        return self._send_input

    @staticmethod
    def _native(events: tuple[_KeyEvent, ...]) -> ctypes.Array:
        array = (_Input * len(events))()
        for record, event in zip(array, events):
            record.type = 1  # INPUT_KEYBOARD
            record.data.ki = _KeyboardInput(event.vk, event.scan, event.flags, 0, 0)
        return array

    def send(
        self, action: InputAction, value: str | None, verify_target: Callable[[], bool],
    ) -> None:
        _validate_input(action, value)
        events: list[_KeyEvent] = []
        if action in _TEXT_ACTIONS:
            encoded = value.encode("utf-16-le")  # type: ignore[union-attr]
            for offset in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[offset:offset + 2], "little")
                down = _KeyEvent(scan=unit, flags=_UNICODE)
                events.extend((down, down.release()))
        if action not in _TEXT_ACTIONS or action is InputAction.TYPE_TEXT_AND_ENTER:
            keys = _KEYS[InputAction.ENTER if action in _TEXT_ACTIONS else action]
            downs = tuple(_KeyEvent(vk=key, flags=_EXTENDED if key in _EXTENDED_KEYS else 0)
                          for key in keys)
            events.extend(downs)
            events.extend(event.release() for event in reversed(downs))
        batch = tuple(events)
        native = self._native(batch)
        sender = self._sender()
        # Keep the authoritative check adjacent to SendInput, after allocation/loading.
        if not verify_target():
            raise InputError("The prepared window is not the verified foreground target. No input sent.")
        try:
            inserted = sender(len(native), native, ctypes.sizeof(_Input))
        except Exception:
            self._release_pressed(sender, tuple(e for e in batch if not e.flags & _KEYUP))
            raise InputError("Keyboard input failed; delivery could not be verified.") from None
        if inserted != len(batch):
            # SendInput inserts a prefix. Release only keys left down by that prefix;
            # never retry the unsent text or shortcut.
            self._release_pressed(sender, batch[:inserted])
            raise InputError("Windows did not accept all keyboard events; input may be partial.")

    def _release_pressed(self, sender: Callable[..., int], events: tuple[_KeyEvent, ...]) -> None:
        held: dict[tuple[int, int], _KeyEvent] = {}
        for event in events:
            key = (event.vk, event.scan)
            if event.flags & _KEYUP:
                held.pop(key, None)
            else:
                held[key] = event
        if not held:
            return
        releases = self._native(tuple(event.release() for event in reversed(tuple(held.values()))))
        try:
            if sender(len(releases), releases, ctypes.sizeof(_Input)) != len(releases):
                raise InputError("Windows refused keyboard cleanup; key release could not be verified.")
        except Exception:
            raise InputError("Windows refused keyboard cleanup; key release could not be verified.") from None


class WindowInputTool:
    name = "window_input"
    risk_level = RiskLevel.SENSITIVE
    allowed_actions = tuple(action.value for action in InputAction)

    def __init__(self, windows: WindowController, inputs: InputController, catalog: AppCatalog,
                 editable_controls: EditableControlResolver | None = None) -> None:
        self._windows = windows
        self._inputs = inputs
        self._catalog = catalog
        self._editable_controls = editable_controls or WindowsEditableControlResolver()

    def _failure(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        if not {"query", "action"}.issubset(arguments) or set(arguments) - {"query", "action", "value"}:
            return self._failure("Invalid window input arguments.")
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failure("Window query must not be empty.")
        try:
            action = InputAction(arguments["action"])
        except (ValueError, TypeError):
            return self._failure("Unsupported input action.")
        value = arguments.get("value")
        try:
            _validate_input(action, value)
        except ValueError as exc:
            return self._failure(str(exc))
        try:
            match = match_window_for_focus(query, self._windows.visible_windows(), self._catalog)
        except Exception:
            return self._failure("Failed to inspect visible windows.")
        if isinstance(match, str):
            return self._failure(match)
        if not match.handle or not match.process_id or not match.title or not match.executable_name:
            return self._failure("The window identity could not be verified.")
        prepared = PreparedWindowInput(match.handle, match.process_id, match.title,
                                       match.executable_name, action, value)
        # Display-only normalized arguments; execution always uses the frozen payload.
        normalized = [("query", f"{bounded_window_label(match.title)} — {bounded_window_label(match.executable_name, 80)}"), ("action", action.value)]
        if value is not None:
            preview = repr(value[:TEXT_PREVIEW_LENGTH])
            if len(value) > TEXT_PREVIEW_LENGTH:
                preview += f" … ({len(value)} characters)"
            normalized.append(("value", preview))
        return ToolPreparation(prepared, ToolArguments(tuple(normalized)))

    @staticmethod
    def confirmation_summary(arguments: ToolArguments) -> str:
        summary = f"Target:\n{arguments['query']}\n\nAction:\n{arguments['action']}"
        if "value" in arguments:
            summary += f"\n\nText:\n{arguments['value']}"
        return summary

    @staticmethod
    def _matches(expected: PreparedWindowInput, current: WindowInfo | None) -> bool:
        return current is not None and (
            current.handle == expected.handle and current.process_id == expected.process_id
            and current.title == expected.title
            and current.executable_name.casefold() == expected.executable_name.casefold()
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedWindowInput):
            return self._failure("The prepared input action is invalid.")
        target = prepared_value
        display_title = bounded_window_label(target.title)
        try:
            _validate_input(target.action, target.value)
            if not self._windows.is_window_valid(target.handle, target.process_id):
                return self._failure("The prepared window is no longer available. No input sent.")
            # Inspect the exact handle, never resolve the query again.
            if not self._matches(target, self._windows.get_window_info(target.handle)):
                return self._failure("The prepared window identity changed. No input sent.")
            if self._windows.is_minimized(target.handle):
                self._windows.restore_window(target.handle)
                if self._windows.is_minimized(target.handle):
                    return self._failure("Windows could not restore the target. No input sent.")
            if not self._windows.set_foreground_window(target.handle):
                return self._failure("Windows refused foreground focus. No input sent.")

            def verify_target() -> bool:
                return (self._windows.is_window_valid(target.handle, target.process_id)
                        and self._matches(target, self._windows.get_foreground_window()))

            if target.action in _TEXT_ACTIONS:
                if not verify_target():
                    return self._failure("The prepared window lost foreground focus. No input sent.")
                with self._editable_controls.focus(target.handle, target.process_id) as editor:
                    if not verify_target() or not editor.is_focused():
                        return self._failure("The editable text control lost focus. No input sent.")
                    # WindowsInputController calls this guard immediately before
                    # SendInput, so a late focus change also sends zero events.
                    self._inputs.send(target.action, target.value,
                                      lambda: verify_target() and editor.is_focused())
            else:
                self._inputs.send(target.action, target.value, verify_target)
        except EditableControlError as exc:
            if str(exc) == "No editable text control could be identified":
                return self._failure(f"No editable text control could be identified in {display_title}.")
            if str(exc) == "Multiple equally suitable editable text controls were found":
                return self._failure(f"Multiple equally suitable editable text controls were found in {display_title}.")
            return self._failure(str(exc))
        except InputError as exc:
            return self._failure(str(exc))
        except Exception:
            # Exception messages/tracebacks from injected backends may contain text.
            logger.warning("Window input failed for target %s", target.title)
            return self._failure("Window input could not be completed safely.")
        count = len(target.value) if target.value is not None else 0
        details = {"title": display_title, "action": target.action.value, "character_count": count}
        if target.action in _TEXT_ACTIONS:
            message = f"Typed {count} characters into {display_title}."
            if target.action is InputAction.TYPE_TEXT_AND_ENTER:
                message += " Pressed Enter."
        else:
            label = ("Ctrl+" + target.action.value[-1].upper() if target.action.value.startswith("ctrl_")
                     else target.action.value.replace("_", " ").title())
            message = f"Pressed {label} in {display_title}."
        return ToolResult(True, message, self.risk_level, details)
