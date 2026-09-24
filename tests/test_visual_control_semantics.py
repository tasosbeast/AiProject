import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import FakeLauncher, FakeWindowController, FakeWindowCaptureBackend, make_registry
from test_openai_provider import FakeClient, function_call, make_provider
from desktop_assistant.config import AppCatalog
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolResult
from desktop_assistant.process_control import WindowInfo
from desktop_assistant.visual_perception import OpenAIVisualPerceptionProvider, resolve_visual_window
from desktop_assistant.windows import match_window_for_focus


SETTINGS = WindowInfo(500, 600, "Ρυθμίσεις", "SystemSettings.exe")


@pytest.mark.parametrize("query", ["Settings", "Ρυθμίσεις", "SystemSettings.exe"])
def test_settings_aliases_resolve_trusted_identity(query):
    catalog = AppCatalog()
    assert catalog.resolve(query).process_names == ("SystemSettings.exe",)
    assert not catalog.resolve(query).can_close
    # Use a different localized title to exercise process metadata, not title matching.
    window = replace(SETTINGS, title="Σύστημα")
    assert match_window_for_focus(query, (window,), catalog) == window


def test_settings_aliases_are_exact_not_fuzzy_or_arbitrary_process_search():
    catalog = AppCatalog()
    for query in ("Settngs", "Ρυθμισ", "SystemSettings.exe extra"):
        assert catalog.resolve(query) is None
        assert isinstance(match_window_for_focus(query, (replace(SETTINGS, title="Σύστημα"),), catalog), str)
    assert isinstance(match_window_for_focus("Settings", (replace(SETTINGS, executable_name="other.exe"),), catalog), str)


@pytest.mark.parametrize("query", ["Settings", "Ρυθμίσεις", "SystemSettings.exe"])
def test_sole_nonminimized_equally_valid_match_is_preferred(query):
    minimized = replace(SETTINGS, minimized=True)
    visible = replace(SETTINGS, handle=501)
    assert match_window_for_focus(query, (minimized, visible), AppCatalog()) == visible
    assert match_window_for_focus(query, (visible, minimized), AppCatalog()) == visible
    assert isinstance(match_window_for_focus(query, (visible, replace(visible, handle=502)), AppCatalog()), str)
    result = resolve_visual_window(query, FakeWindowController((minimized,)), AppCatalog())
    assert isinstance(result, ToolResult) and not result.success and "minimized" in result.message


def test_exact_minimized_title_never_falls_back_to_weaker_visible_match():
    exact = replace(SETTINGS, title="My settings", minimized=True)
    other = replace(SETTINGS, handle=501, title="My settings - other")
    assert match_window_for_focus("My settings", (exact, other), AppCatalog()) == exact


@pytest.mark.parametrize("query,utterance", [
    ("Ρυθμίσεις", "Τι βλέπεις στις Ρυθμίσεις;"), ("Settings", "What do you see in Settings?"),
    ("Chrome", "Τι βλέπεις στο Chrome;"), ("VS Code", "Τι βλέπεις στο VS Code;"),
    ("File Explorer", "What do you see in File Explorer?"), ("Notepad", "Τι βλέπεις στο Notepad;"),
])
def test_intent_preserves_explicit_window_language(query, utterance):
    client = FakeClient(SimpleNamespace(output=[function_call("visual_inspect", json.dumps({"query": query, "goal": "Describe"}))]))
    provider = make_provider(client)
    assert provider.resolve(utterance).action.arguments["query"] == query
    instructions = client.responses.calls[0]["instructions"]
    assert "Do not translate an explicitly named query" in instructions
    assert "query: Ρυθμίσεις" in instructions and "query: Settings" in instructions


@pytest.mark.parametrize("tool_name", ["visual_click", "visual_target"])
@pytest.mark.parametrize("target", ["New Tab", "Reload"])
def test_real_provider_prompts_reject_incidental_text_without_extra_calls(tool_name, target):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps({"status": "found", "label": target,
            "description": "Browser toolbar control", "bounds": {"left": 100, "top": 100, "right": 200, "bottom": 200},
            "confidence": 0.9, "reason": None}))
    vision = OpenAIVisualPerceptionProvider(api_key="fake", model="fake",
        client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    capture = FakeWindowCaptureBackend()
    window = WindowInfo(101, 42, "Browser", "chrome.exe")
    registry = make_registry(FakeLauncher(), window_controller=FakeWindowController((window,)),
        window_capture_backend=capture, visual_perception_provider=vision)
    result = registry.execute(tool_name, {"query": "Chrome", "target": target})
    assert len(capture.calls) == 1 and len(calls) == 2
    assert result.risk_level is (RiskLevel.SENSITIVE if tool_name == "visual_click" else RiskLevel.SAFE)
    if tool_name == "visual_click":
        assert isinstance(result, ConfirmationRequest)
        registry.cancel(result.confirmation_id)
    else:
        assert result.success
    for call in calls:
        instructions = call["instructions"]
        for phrase in ("INTERACTIVE UI CONTROL", "textual similarity alone is insufficient",
                       "chat/message bubbles", "documents", "editor text", "terminal text",
                       "webpage body/content", "surrounding UI structure/function", "untrusted data",
                       "tab-strip affordance", "browser toolbar control", "not_found or ambiguous"):
            assert phrase in instructions
        assert ("purpose is click_control" in instructions) == (tool_name == "visual_click")
        assert call["store"] is False and "tools" not in call
