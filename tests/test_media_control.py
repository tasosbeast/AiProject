from __future__ import annotations

import pytest

from desktop_assistant.config import AppCatalog
from desktop_assistant.intent.models import ActionPlan, IntentKind, IntentResult, ToolAction
from desktop_assistant.media_control import (
    MediaAction,
    MediaControlTool,
    VolumeAction,
    VolumeControlTool,
    WindowsMediaController,
    _VOLUME_VK_MAP,
    _MEDIA_VK_MAP,
)
from desktop_assistant.models import RiskLevel, ToolArguments
from desktop_assistant.router import CommandRouter, DeterministicAction
from conftest import FakeLauncher, FakeMediaController, make_registry
from test_assistant_plan import FakeProvider, make_test_assistant


def test_volume_control_tool_executes_valid_actions() -> None:
    controller = FakeMediaController()
    tool = VolumeControlTool(controller)

    for action_str, expected_enum, expected_msg in [
        ("volume_up", VolumeAction.VOLUME_UP, "Increased volume."),
        ("volume_down", VolumeAction.VOLUME_DOWN, "Decreased volume."),
        ("mute_toggle", VolumeAction.MUTE_TOGGLE, "Toggled mute."),
    ]:
        prep = tool.prepare(ToolArguments((("action", action_str),)))
        assert not isinstance(prep, ToolResult if hasattr(prep, "success") else type(None))
        assert prep.execution_value == action_str

        result = tool.execute(prep.execution_value)
        assert result.success
        assert result.risk_level == RiskLevel.SAFE
        assert result.message == expected_msg
        assert result.details == {"action": action_str}

    assert controller.volume_actions == [
        VolumeAction.VOLUME_UP,
        VolumeAction.VOLUME_DOWN,
        VolumeAction.MUTE_TOGGLE,
    ]


def test_volume_control_tool_rejects_invalid_actions() -> None:
    controller = FakeMediaController()
    tool = VolumeControlTool(controller)

    prep = tool.prepare(ToolArguments((("action", "invalid_action"),)))
    assert not prep.success
    assert "Invalid volume action" in prep.message

    exec_result = tool.execute("invalid_action")
    assert not exec_result.success
    assert "The prepared volume action is invalid." in exec_result.message

    bad_type_result = tool.execute(12345)
    assert not bad_type_result.success
    assert "The prepared volume action is invalid." in bad_type_result.message

    assert controller.volume_actions == []


def test_media_control_tool_executes_valid_actions() -> None:
    controller = FakeMediaController()
    tool = MediaControlTool(controller)

    for action_str, expected_enum, expected_msg in [
        ("play_pause", MediaAction.PLAY_PAUSE, "Toggled media playback."),
        ("next_track", MediaAction.NEXT_TRACK, "Skipped to next track."),
        ("previous_track", MediaAction.PREVIOUS_TRACK, "Returned to previous track."),
    ]:
        prep = tool.prepare(ToolArguments((("action", action_str),)))
        assert not isinstance(prep, ToolResult if hasattr(prep, "success") else type(None))
        assert prep.execution_value == action_str

        result = tool.execute(prep.execution_value)
        assert result.success
        assert result.risk_level == RiskLevel.SAFE
        assert result.message == expected_msg
        assert result.details == {"action": action_str}

    assert controller.media_actions == [
        MediaAction.PLAY_PAUSE,
        MediaAction.NEXT_TRACK,
        MediaAction.PREVIOUS_TRACK,
    ]


def test_media_control_tool_rejects_invalid_actions() -> None:
    controller = FakeMediaController()
    tool = MediaControlTool(controller)

    prep = tool.prepare(ToolArguments((("action", "stop"),)))
    assert not prep.success
    assert "Invalid media action" in prep.message

    exec_result = tool.execute("stop")
    assert not exec_result.success
    assert "The prepared media action is invalid." in exec_result.message

    assert controller.media_actions == []


def test_windows_media_controller_sends_exact_virtual_keys() -> None:
    recorded_events: list[int] = []

    def fake_keybd_event(vk: int) -> None:
        recorded_events.append(vk)

    controller = WindowsMediaController(keybd_event_fn=fake_keybd_event)

    controller.send_volume(VolumeAction.VOLUME_UP)
    controller.send_volume(VolumeAction.VOLUME_DOWN)
    controller.send_volume(VolumeAction.MUTE_TOGGLE)

    controller.send_media(MediaAction.PLAY_PAUSE)
    controller.send_media(MediaAction.NEXT_TRACK)
    controller.send_media(MediaAction.PREVIOUS_TRACK)

    expected_vk_codes = [
        0xAF,  # VK_VOLUME_UP
        0xAE,  # VK_VOLUME_DOWN
        0xAD,  # VK_VOLUME_MUTE
        0xB3,  # VK_MEDIA_PLAY_PAUSE
        0xB0,  # VK_MEDIA_NEXT_TRACK
        0xB1,  # VK_MEDIA_PREV_TRACK
    ]
    assert recorded_events == expected_vk_codes


def test_windows_media_controller_rejects_arbitrary_inputs() -> None:
    recorded_events: list[int] = []
    controller = WindowsMediaController(keybd_event_fn=lambda vk: recorded_events.append(vk))

    with pytest.raises(ValueError):
        controller.send_volume("destroy_speakers")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        controller.send_media("fast_forward")  # type: ignore[arg-type]

    assert recorded_events == []


def test_router_deterministic_volume_and_media_commands() -> None:
    launcher = FakeLauncher()
    registry = make_registry(launcher)
    router = CommandRouter(registry, AppCatalog())

    cases = [
        ("volume up", "volume_control", {"action": "volume_up"}),
        ("increase volume", "volume_control", {"action": "volume_up"}),
        ("turn up volume", "volume_control", {"action": "volume_up"}),
        ("turn up the volume", "volume_control", {"action": "volume_up"}),
        ("volume down", "volume_control", {"action": "volume_down"}),
        ("decrease volume", "volume_control", {"action": "volume_down"}),
        ("turn down volume", "volume_control", {"action": "volume_down"}),
        ("turn down the volume", "volume_control", {"action": "volume_down"}),
        ("mute", "volume_control", {"action": "mute_toggle"}),
        ("unmute", "volume_control", {"action": "mute_toggle"}),
        ("toggle mute", "volume_control", {"action": "mute_toggle"}),
        ("mute volume", "volume_control", {"action": "mute_toggle"}),
        ("play", "media_control", {"action": "play_pause"}),
        ("pause", "media_control", {"action": "play_pause"}),
        ("resume", "media_control", {"action": "play_pause"}),
        ("play/pause", "media_control", {"action": "play_pause"}),
        ("play pause", "media_control", {"action": "play_pause"}),
        ("toggle playback", "media_control", {"action": "play_pause"}),
        ("next track", "media_control", {"action": "next_track"}),
        ("next song", "media_control", {"action": "next_track"}),
        ("skip track", "media_control", {"action": "next_track"}),
        ("skip song", "media_control", {"action": "next_track"}),
        ("previous track", "media_control", {"action": "previous_track"}),
        ("previous song", "media_control", {"action": "previous_track"}),
        ("prev track", "media_control", {"action": "previous_track"}),
        ("prev song", "media_control", {"action": "previous_track"}),
    ]

    for command, expected_tool, expected_args in cases:
        decision = router.route_detailed(command)
        assert decision.recognized, f"Failed to recognize '{command}'"
        assert decision.action == DeterministicAction(expected_tool, expected_args), f"Mismatch for '{command}'"


def test_multi_step_plan_with_open_app_and_media_control() -> None:
    launcher = FakeLauncher()
    media_controller = FakeMediaController()
    plan = ActionPlan(
        (
            ToolAction("open_app", {"app_name": "Spotify"}),
            ToolAction("media_control", {"action": "play_pause"}),
        )
    )
    provider = FakeProvider(IntentResult.action_plan(plan.actions))
    assistant = make_test_assistant(
        launcher,
        provider,
        media_controller=media_controller,
    )

    response = assistant.handle("Άνοιξε το Spotify και βάλε μουσική.")

    assert response.kind.value == "completed"
    assert len(launcher.apps) == 1
    assert launcher.apps[0].display_name == "Spotify"
    assert media_controller.media_actions == [MediaAction.PLAY_PAUSE]
    assert "Opening Spotify." in response.message
    assert "Toggled media playback." in response.message


def test_multi_step_plan_with_volume_controls() -> None:
    launcher = FakeLauncher()
    media_controller = FakeMediaController()
    plan = ActionPlan(
        (
            ToolAction("volume_control", {"action": "volume_up"}),
            ToolAction("volume_control", {"action": "volume_up"}),
        )
    )
    provider = FakeProvider(IntentResult.action_plan(plan.actions))
    assistant = make_test_assistant(
        launcher,
        provider,
        media_controller=media_controller,
    )

    response = assistant.handle("Δυνάμωσε τη φωνή δύο φορές.")

    assert response.kind.value == "completed"
    assert media_controller.volume_actions == [
        VolumeAction.VOLUME_UP,
        VolumeAction.VOLUME_UP,
    ]
