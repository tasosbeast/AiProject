"""Confirmed experimental single left click; coordinates are trusted-local only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from typing import Callable, Protocol

from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult
from desktop_assistant.process_control import WindowController
from desktop_assistant.visual_perception import (
    NormalizedVisualBounds, PreparedVisualLocationTarget, VisualTargetStatus,
    VisualTargetTool, revalidate_visual_target_window,
)
from desktop_assistant.windows import bounded_window_label


@dataclass(frozen=True, slots=True, repr=False)
class WindowRectangle:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if (any(type(v) is not int or not -(2 ** 31) <= v < 2 ** 31
                for v in (self.left, self.top, self.right, self.bottom))
                or self.left >= self.right or self.top >= self.bottom):
            raise ValueError("Invalid window rectangle.")


def _click_point(bounds: NormalizedVisualBounds, rectangle: WindowRectangle) -> tuple[int, int]:
    bounds.__post_init__()
    rectangle.__post_init__()
    # Round the center half-up in trusted code; never clip invalid points.
    x = rectangle.left + ((bounds.left + bounds.right) * (rectangle.right - rectangle.left) + 1000) // 2000
    y = rectangle.top + ((bounds.top + bounds.bottom) * (rectangle.bottom - rectangle.top) + 1000) // 2000
    if not (rectangle.left <= x < rectangle.right and rectangle.top <= y < rectangle.bottom):
        raise ValueError("The target point is outside the prepared window.")
    return x, y


class MouseClickError(RuntimeError):
    """A fixed, content-free mouse delivery failure."""


class MouseClickController(Protocol):
    def get_window_rect(self, handle: int) -> WindowRectangle: ...
    def root_at_point(self, point: tuple[int, int]) -> int: ...
    def move_cursor(self, point: tuple[int, int]) -> bool: ...
    def get_cursor_pos(self) -> tuple[int, int]: ...
    def left_click(self, verify_target: Callable[[], bool]) -> None: ...


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("data", _InputUnion)]


class WindowsMouseClickController:
    """Lazy Win32 backend with an injectable API; never loaded by fake unit tests."""

    def __init__(self, api: object | None = None) -> None:
        self._api = api

    def _native_api(self):
        if self._api is None:
            if os.name != "nt":
                raise MouseClickError("Mouse clicking is available only on Windows.")
            api = ctypes.WinDLL("user32", use_last_error=True)
            api.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
            api.GetWindowRect.restype = wintypes.BOOL
            api.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
            api.SetCursorPos.restype = wintypes.BOOL
            api.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
            api.GetCursorPos.restype = wintypes.BOOL
            api.WindowFromPoint.argtypes = (wintypes.POINT,)
            api.WindowFromPoint.restype = wintypes.HWND
            api.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
            api.GetAncestor.restype = wintypes.HWND
            api.SendInput.argtypes = (ctypes.c_uint32, ctypes.POINTER(_Input), ctypes.c_int)
            api.SendInput.restype = ctypes.c_uint32
            self._api = api
        return self._api

    def get_window_rect(self, handle: int) -> WindowRectangle:
        rect = wintypes.RECT()
        if not self._native_api().GetWindowRect(handle, ctypes.byref(rect)):
            raise MouseClickError("The window rectangle could not be verified.")
        return WindowRectangle(rect.left, rect.top, rect.right, rect.bottom)

    def root_at_point(self, point: tuple[int, int]) -> int:
        api = self._native_api()
        handle = api.WindowFromPoint(wintypes.POINT(*point))
        return int(api.GetAncestor(handle, 2) or 0) if handle else 0  # GA_ROOT

    def move_cursor(self, point: tuple[int, int]) -> bool:
        return bool(self._native_api().SetCursorPos(*point))

    def get_cursor_pos(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not self._native_api().GetCursorPos(ctypes.byref(point)):
            raise MouseClickError("The cursor position could not be verified.")
        return point.x, point.y

    @staticmethod
    def _events(flags: tuple[int, ...]):
        events = (_Input * len(flags))()
        for event, flag in zip(events, flags):
            event.type = 0  # INPUT_MOUSE; no movement, coordinates or extra buttons
            event.data.mi.dwFlags = flag
        return events

    def left_click(self, verify_target: Callable[[], bool]) -> None:
        api = self._native_api()
        events = self._events((0x0002, 0x0004))  # LEFTDOWN, LEFTUP
        if not verify_target():
            raise MouseClickError("The prepared target changed. No click sent.")
        try:
            accepted = api.SendInput(2, events, ctypes.sizeof(_Input))
        except Exception:
            accepted = -1  # Unknown delivery: release once, never replay the click.
        if accepted == 2:
            return
        if accepted == 0:
            raise MouseClickError("Windows did not accept the click. No click sent.")
        cleanup = self._events((0x0004,))
        try:
            released = api.SendInput(1, cleanup, ctypes.sizeof(_Input)) == 1
        except Exception:
            released = False
        if not released:
            raise MouseClickError("Click delivery may have been partial; left-button release could not be verified.")
        raise MouseClickError("Click delivery may have been partial; left-button release was sent. The click was not retried.")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedVisualClick:
    handle: int
    process_id: int
    title: str
    executable_name: str
    target: str
    label: str
    description: str
    bounds: NormalizedVisualBounds
    rectangle: WindowRectangle


class VisualClickTool:
    name = "visual_click"
    risk_level = RiskLevel.SENSITIVE

    def __init__(self, windows: WindowController, targeting: VisualTargetTool,
                 mouse: MouseClickController) -> None:
        self._windows = windows
        self._targeting = targeting
        self._mouse = mouse

    def _failure(self, message: str) -> ToolResult:
        return ToolResult(False, message, self.risk_level)

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        if set(arguments) != {"query", "target"}:
            return self._failure("An explicit window and visual target are required.")
        try:
            preparation = self._targeting.prepare(arguments)
            if isinstance(preparation, ToolResult):
                return self._failure(preparation.message)
            target = preparation.execution_value
            if not isinstance(target, PreparedVisualLocationTarget):
                return self._failure("The visual target could not be prepared.")
            before = self._mouse.get_window_rect(target.handle)
            before.__post_init__()
            located = self._targeting.locate_prepared(target)
            after = self._mouse.get_window_rect(target.handle)
            if before != after or not self._valid_window(target):
                return self._failure("The window changed during visual targeting. Request the click again.")
            if isinstance(located, ToolResult):
                return self._failure("The visual target could not be verified.")
            result, _, _ = located
            if result.status is not VisualTargetStatus.FOUND:
                return self._failure("The visual target was not found or was ambiguous. No click prepared.")
            result.__post_init__()
            result.bounds.__post_init__()
            _click_point(result.bounds, before)
            prepared = PreparedVisualClick(
                target.handle, target.process_id, target.title, target.executable_name,
                target.target, bounded_window_label(result.label, 200),
                bounded_window_label(result.description, 500), result.bounds, before,
            )
            return ToolPreparation(prepared, ToolArguments((
                ("query", f"{bounded_window_label(target.title)} — {bounded_window_label(target.executable_name, 80)}"),
                ("target", f"{prepared.label} — {prepared.description}"),
            )))
        except Exception:
            return self._failure("The visual click could not be prepared safely.")

    @staticmethod
    def confirmation_summary(arguments: ToolArguments) -> str:
        return (f"Target window:\n{arguments['query']}\n\nVisual target:\n{arguments['target']}"
                "\n\nAction:\nExperimental single left click")

    def _valid_window(self, target: PreparedVisualClick | PreparedVisualLocationTarget) -> bool:
        return revalidate_visual_target_window(
            self._windows, target.handle, target.process_id, target.title, target.executable_name,
        )

    def _foreground_matches(self, target: PreparedVisualClick) -> bool:
        current = self._windows.get_foreground_window()
        return current is not None and (
            current.handle == target.handle and current.process_id == target.process_id
            and current.title == target.title and not current.minimized
            and current.executable_name.casefold() == target.executable_name.casefold()
        )

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, PreparedVisualClick):
            return self._failure("The prepared visual click is invalid.")
        target = prepared_value
        try:
            if not self._valid_window(target) or self._mouse.get_window_rect(target.handle) != target.rectangle:
                return self._failure("The prepared window changed or moved. No click sent.")
            if not self._windows.set_foreground_window(target.handle) or not self._foreground_matches(target):
                return self._failure("Windows could not verify foreground focus. No click sent.")
            point = _click_point(target.bounds, target.rectangle)

            def verify_target() -> bool:
                return (self._valid_window(target)
                        and self._mouse.get_window_rect(target.handle) == target.rectangle
                        and self._foreground_matches(target)
                        and self._mouse.root_at_point(point) == target.handle)

            if not verify_target():
                return self._failure("The point is not owned by the prepared foreground window. No click sent.")
            if not self._mouse.move_cursor(point) or self._mouse.get_cursor_pos() != point:
                return self._failure("The cursor position could not be verified. No click sent.")
            self._mouse.left_click(lambda: verify_target() and self._mouse.get_cursor_pos() == point)
        except MouseClickError as exc:
            return self._failure(str(exc))
        except Exception:
            return self._failure("The visual click could not be completed safely.")
        title = bounded_window_label(target.title)
        return ToolResult(True, f"Clicked visual target '{target.label}' in {title}.", self.risk_level,
                          {"title": title, "requested_target": target.target, "resolved_label": target.label})
