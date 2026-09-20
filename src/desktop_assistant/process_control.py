from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Protocol

from desktop_assistant.config import AppDefinition


class ProcessControlError(RuntimeError):
    """A safe process-inspection or graceful-close failure."""


@dataclass(frozen=True, slots=True)
class CloseRequestResult:
    was_running: bool
    requested_window_count: int


class AppProcessController(Protocol):
    def is_running(self, app: AppDefinition) -> bool: ...

    def request_close(self, app: AppDefinition) -> CloseRequestResult: ...


@dataclass(frozen=True, slots=True)
class _ProcessEntry:
    process_id: int
    executable_name: str


@dataclass(frozen=True, slots=True)
class _WindowEntry:
    handle: int
    process_id: int


class _ProcessApi(Protocol):
    def processes(self) -> tuple[_ProcessEntry, ...]: ...

    def top_level_windows(self) -> tuple[_WindowEntry, ...]: ...

    def post_close(self, window_handle: int) -> bool: ...


class WindowsAppProcessController:
    """Narrow allowlisted process status and graceful-window-close boundary."""

    def __init__(self, api: _ProcessApi | None = None) -> None:
        self._api = api or _Win32ProcessApi()

    def is_running(self, app: AppDefinition) -> bool:
        return bool(self._matching_process_ids(app))

    def request_close(self, app: AppDefinition) -> CloseRequestResult:
        process_ids = self._matching_process_ids(app)
        if not process_ids:
            return CloseRequestResult(False, 0)

        try:
            windows = self._api.top_level_windows()
        except OSError as exc:
            raise ProcessControlError("Windows could not inspect application windows.") from exc

        matching_windows = tuple(
            window for window in windows if window.process_id in process_ids
        )
        requested = 0
        for window in matching_windows:
            try:
                accepted = self._api.post_close(window.handle)
            except OSError as exc:
                raise ProcessControlError(
                    "Windows could not request the application to close."
                ) from exc
            if not accepted:
                raise ProcessControlError(
                    "Windows could not request the application to close."
                )
            requested += 1
        return CloseRequestResult(True, requested)

    def _matching_process_ids(self, app: AppDefinition) -> frozenset[int]:
        trusted_names = frozenset(name.casefold() for name in app.process_names)
        if not trusted_names:
            return frozenset()
        try:
            processes = self._api.processes()
        except OSError as exc:
            raise ProcessControlError("Windows could not inspect application processes.") from exc
        return frozenset(
            process.process_id
            for process in processes
            if process.executable_name.casefold() in trusted_names
        )


class _Win32ProcessApi:
    _TH32CS_SNAPPROCESS = 0x00000002
    _WM_CLOSE = 0x0010
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self) -> None:
        if os.name != "nt":
            raise ProcessControlError("Application process control is only available on Windows.")
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = (
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            )

        self._entry_type = ProcessEntry32W
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        self._kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Process32FirstW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry32W),
        )
        self._kernel32.Process32FirstW.restype = wintypes.BOOL
        self._kernel32.Process32NextW.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ProcessEntry32W),
        )
        self._kernel32.Process32NextW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._user32.EnumWindows.argtypes = (self._enum_callback_type, wintypes.LPARAM)
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.PostMessageW.restype = wintypes.BOOL

    def processes(self) -> tuple[_ProcessEntry, ...]:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(self._TH32CS_SNAPPROCESS, 0)
        if snapshot == self._INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        entries: list[_ProcessEntry] = []
        try:
            entry = self._entry_type()
            entry.dwSize = ctypes.sizeof(self._entry_type)
            if not self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                raise ctypes.WinError(ctypes.get_last_error())
            while True:
                entries.append(_ProcessEntry(int(entry.th32ProcessID), entry.szExeFile))
                ctypes.set_last_error(0)
                if not self._kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    error_code = ctypes.get_last_error()
                    if error_code not in {0, 18}:  # ERROR_NO_MORE_FILES
                        raise ctypes.WinError(error_code)
                    break
        finally:
            self._kernel32.CloseHandle(snapshot)
        return tuple(entries)

    def top_level_windows(self) -> tuple[_WindowEntry, ...]:
        from ctypes import wintypes

        windows: list[_WindowEntry] = []

        @self._enum_callback_type
        def collect(window_handle: int, _parameter: int) -> bool:
            if not self._user32.IsWindowVisible(window_handle):
                return True
            process_id = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
            if process_id.value:
                windows.append(_WindowEntry(int(window_handle), int(process_id.value)))
            return True

        ctypes.set_last_error(0)
        if not self._user32.EnumWindows(collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return tuple(windows)

    def post_close(self, window_handle: int) -> bool:
        return bool(self._user32.PostMessageW(window_handle, self._WM_CLOSE, 0, 0))
