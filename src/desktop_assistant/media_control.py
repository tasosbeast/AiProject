from __future__ import annotations

import ctypes
from enum import Enum
import logging
import os
from types import MappingProxyType
from typing import Callable, Protocol

from desktop_assistant.models import RiskLevel, ToolArguments, ToolPreparation, ToolResult


logger = logging.getLogger(__name__)


class VolumeAction(str, Enum):
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    MUTE_TOGGLE = "mute_toggle"


class MediaAction(str, Enum):
    PLAY_PAUSE = "play_pause"
    NEXT_TRACK = "next_track"
    PREVIOUS_TRACK = "previous_track"


# Virtual-Key codes from Windows user32
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3

_VOLUME_VK_MAP: MappingProxyType[VolumeAction, int] = MappingProxyType(
    {
        VolumeAction.VOLUME_UP: _VK_VOLUME_UP,
        VolumeAction.VOLUME_DOWN: _VK_VOLUME_DOWN,
        VolumeAction.MUTE_TOGGLE: _VK_VOLUME_MUTE,
    }
)

_MEDIA_VK_MAP: MappingProxyType[MediaAction, int] = MappingProxyType(
    {
        MediaAction.PLAY_PAUSE: _VK_MEDIA_PLAY_PAUSE,
        MediaAction.NEXT_TRACK: _VK_MEDIA_NEXT_TRACK,
        MediaAction.PREVIOUS_TRACK: _VK_MEDIA_PREV_TRACK,
    }
)

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002


class MediaController(Protocol):
    def send_volume(self, action: VolumeAction) -> None: ...

    def send_media(self, action: MediaAction) -> None: ...


def _default_keybd_event(vk_code: int) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows media control is only available on Windows.")
    user32 = ctypes.windll.user32
    user32.keybd_event(vk_code, 0, _KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk_code, 0, _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP, 0)


class WindowsMediaController:
    """Controls Windows system volume and media playback via fixed hardware virtual keys."""

    def __init__(self, keybd_event_fn: Callable[[int], None] | None = None) -> None:
        self._keybd_event = keybd_event_fn or _default_keybd_event

    def send_volume(self, action: VolumeAction) -> None:
        if not isinstance(action, VolumeAction):
            try:
                action = VolumeAction(str(action))
            except ValueError:
                raise ValueError(f"Invalid volume action: {action}")
        vk = _VOLUME_VK_MAP[action]
        self._keybd_event(vk)

    def send_media(self, action: MediaAction) -> None:
        if not isinstance(action, MediaAction):
            try:
                action = MediaAction(str(action))
            except ValueError:
                raise ValueError(f"Invalid media action: {action}")
        vk = _MEDIA_VK_MAP[action]
        self._keybd_event(vk)


class VolumeControlTool:
    name = "volume_control"
    risk_level = RiskLevel.SAFE

    def __init__(self, controller: MediaController) -> None:
        self._controller = controller

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        raw_action = arguments.get("action")
        try:
            action = VolumeAction(str(raw_action))
        except ValueError:
            return ToolResult(
                False,
                f"Invalid volume action '{raw_action}'. Supported actions: volume_up, volume_down, mute_toggle.",
                self.risk_level,
            )
        return ToolPreparation(action.value, ToolArguments((("action", action.value),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, str):
            return ToolResult(False, "The prepared volume action is invalid.", self.risk_level)
        try:
            action = VolumeAction(prepared_value)
        except ValueError:
            return ToolResult(False, "The prepared volume action is invalid.", self.risk_level)

        try:
            self._controller.send_volume(action)
        except Exception:
            logger.exception("Failed to execute volume action: %s", action.value)
            return ToolResult(False, "Failed to control volume.", self.risk_level)

        messages = {
            VolumeAction.VOLUME_UP: "Increased volume.",
            VolumeAction.VOLUME_DOWN: "Decreased volume.",
            VolumeAction.MUTE_TOGGLE: "Toggled mute.",
        }
        return ToolResult(True, messages[action], self.risk_level, {"action": action.value})


class MediaControlTool:
    name = "media_control"
    risk_level = RiskLevel.SAFE

    def __init__(self, controller: MediaController) -> None:
        self._controller = controller

    def prepare(self, arguments: ToolArguments) -> ToolPreparation | ToolResult:
        raw_action = arguments.get("action")
        try:
            action = MediaAction(str(raw_action))
        except ValueError:
            return ToolResult(
                False,
                f"Invalid media action '{raw_action}'. Supported actions: play_pause, next_track, previous_track.",
                self.risk_level,
            )
        return ToolPreparation(action.value, ToolArguments((("action", action.value),)))

    def execute(self, prepared_value: object) -> ToolResult:
        if not isinstance(prepared_value, str):
            return ToolResult(False, "The prepared media action is invalid.", self.risk_level)
        try:
            action = MediaAction(prepared_value)
        except ValueError:
            return ToolResult(False, "The prepared media action is invalid.", self.risk_level)

        try:
            self._controller.send_media(action)
        except Exception:
            logger.exception("Failed to execute media action: %s", action.value)
            return ToolResult(False, "Failed to control media playback.", self.risk_level)

        messages = {
            MediaAction.PLAY_PAUSE: "Toggled media playback.",
            MediaAction.NEXT_TRACK: "Skipped to next track.",
            MediaAction.PREVIOUS_TRACK: "Returned to previous track.",
        }
        return ToolResult(True, messages[action], self.risk_level, {"action": action.value})
