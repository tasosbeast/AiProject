import base64
import json
from types import SimpleNamespace

import pytest

from conftest import (
    FakeLauncher,
    FakeMouseClickController,
    FakeVisualPerceptionProvider,
    FakeWindowCaptureBackend,
    FakeWindowController,
    make_registry,
)
from test_visual_perception import TARGET
from desktop_assistant.config import AppCatalog
from desktop_assistant.models import ConfirmationRequest, RiskLevel, ToolResult
from desktop_assistant.visual_click import VisualClickTool
from desktop_assistant.visual_perception import (
    MAX_IMAGE_BYTES,
    MAX_TARGET_CHARS,
    MalformedVisualPerceptionResponseError,
    NormalizedVisualBounds,
    OpenAIVisualPerceptionProvider,
    VisualPerceptionUnavailableError,
    VisualTargetResult,
    VisualTargetStatus,
    VisualTargetTool,
    _target_crop,
    _global_target_bounds,
)


ARGS = {"query": "VS Code", "target": "New Tab"}


def _harness(provider=None, capture=None, windows=None, mouse=None):
    win = windows or FakeWindowController((TARGET,), active_window=TARGET)
    m = mouse or FakeMouseClickController()
    cap = capture or FakeWindowCaptureBackend()
    vision = provider or FakeVisualPerceptionProvider()
    registry = make_registry(
        FakeLauncher(),
        window_controller=win,
        mouse_click_controller=m,
        window_capture_backend=cap,
        visual_perception_provider=vision,
    )
    return registry, win, m, cap, vision


def test_refinement_receives_two_images_in_exact_order():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(
                dict(
                    status="found",
                    label="+",
                    description="New Tab button in sidebar",
                    bounds=dict(left=200, top=300, right=400, bottom=500),
                    confidence=0.96,
                    reason="Verified as Chrome New Tab affordance from layout context.",
                )
            )
        )

    provider = OpenAIVisualPerceptionProvider(
        api_key="fake",
        model="fake",
        client=SimpleNamespace(responses=SimpleNamespace(create=create)),
    )

    full_png = b"full_window_screenshot_bytes"
    crop_png = b"trusted_local_crop_bytes"
    result = provider.refine_control(full_png, crop_png, "New Tab")

    assert len(calls) == 1
    req = calls[0]
    assert req["store"] is False
    assert req["max_output_tokens"] == 1200
    assert req["reasoning"] == {"effort": "low"}
    assert "tools" not in req
    assert req["text"]["format"]["strict"] is True

    content = req["input"][0]["content"]
    assert len(content) == 3

    # 1. Bounded target input_text
    assert content[0]["type"] == "input_text"
    assert content[0]["text"] == "New Tab"

    # 2. Image 1 is the full screenshot
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"] == f"data:image/png;base64,{base64.b64encode(full_png).decode('ascii')}"

    # 3. Image 2 is the local crop
    assert content[2]["type"] == "input_image"
    assert content[2]["image_url"] == f"data:image/png;base64,{base64.b64encode(crop_png).decode('ascii')}"

    instructions = req["instructions"]
    # Verify semantic instructions
    assert "Image 1 is the full captured window and exists only for application and layout context" in instructions
    assert "Image 2 is the trusted local crop and is the image whose target geometry must be returned" in instructions
    assert "Returned bounds MUST be relative ONLY to Image 2 / crop" in instructions
    assert "Do not return coordinates for Image 1" in instructions
    assert "Do not assume the coarse result was correct" in instructions
    assert "Do not reject a control merely because its position differs from a conventional layout" in instructions
    assert "Judge function from the supplied visual context" in instructions
    assert "untrusted" in instructions
    assert "No action was performed" in instructions

    # Ensure coarse confidence is not passed in request
    assert "0.9" not in str(req["input"])
    assert "confidence" not in str(req["input"][0]["content"][0])

    assert result.status == VisualTargetStatus.FOUND
    assert result.label == "+"


def test_visual_click_pipeline_exact_counts_and_context_propagation():
    backend = FakeWindowCaptureBackend()
    vision = FakeVisualPerceptionProvider()
    registry, _, mouse, capture, _ = _harness(provider=vision, capture=backend)

    # Coarse pass finds a control
    coarse_bounds = NormalizedVisualBounds(12, 100, 36, 140)
    vision.target_result = VisualTargetResult(
        VisualTargetStatus.FOUND,
        "+",
        "Plus icon in vertical sidebar",
        coarse_bounds,
        0.92,
    )

    request = registry.execute("visual_click", ARGS)
    assert isinstance(request, ConfirmationRequest)
    assert registry.has_pending_confirmation()

    # Exact call budgets: exactly 1 capture, exactly 1 coarse call, exactly 1 refinement call
    assert len(capture.calls) == 1
    assert len(vision.target_calls) == 1
    assert len(vision.context_refinement_calls) == 1
    assert len(vision.refinement_calls) == 1

    # Verify context refinement received both full window and crop in order
    full_received, crop_received, target_received = vision.context_refinement_calls[0]
    assert full_received == backend.capture.png_bytes
    assert target_received == "New Tab"

    expected_crop, _ = _target_crop(backend.capture.png_bytes, 800, 600, coarse_bounds)
    assert crop_received == expected_crop

    # No third provider call, no recapturing
    assert not mouse.moves and mouse.clicks == 0


def test_unconventional_layout_control_found_and_clicked():
    backend = FakeWindowCaptureBackend()
    vision = FakeVisualPerceptionProvider()
    registry, _, mouse, capture, _ = _harness(provider=vision, capture=backend)

    # Coarse finds + in sidebar
    coarse_bounds = NormalizedVisualBounds(10, 200, 40, 240)
    vision.locate_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.FOUND,
        "+",
        "Plus button below vertical app icons",
        coarse_bounds,
        0.91,
    )

    # Refine confirms it using full context and crop
    refine_bounds = NormalizedVisualBounds(200, 200, 400, 400)
    def refine_with_ctx(full_png, crop_png, target):
        assert full_png == backend.capture.png_bytes
        assert target == "New Tab"
        return VisualTargetResult(
            VisualTargetStatus.FOUND,
            "New Tab",
            "New tab button in browser vertical sidebar",
            refine_bounds,
            0.98,
        )

    vision.refine_control = refine_with_ctx

    request = registry.execute("visual_click", ARGS)
    assert isinstance(request, ConfirmationRequest)
    assert "New Tab" in request.summary
    assert "Experimental single left click" in request.summary

    # Ensure no raw base64 or secrets in summary
    assert "base64" not in request.summary
    assert "NormalizedVisualBounds" not in request.summary

    # Confirm and execute click
    result = registry.confirm(request.confirmation_id)
    assert result.success
    assert mouse.clicks == 1
    assert len(mouse.moves) == 1
    assert "Clicked visual target 'New Tab'" in result.message


def test_refinement_nonfound_and_ambiguous_fail_closed_no_coarse_fallback():
    for status, reason in [
        (VisualTargetStatus.NOT_FOUND, "Not visible in crop."),
        (VisualTargetStatus.AMBIGUOUS, "Multiple matches in crop."),
    ]:
        backend = FakeWindowCaptureBackend()
        vision = FakeVisualPerceptionProvider()
        registry, _, mouse, capture, _ = _harness(provider=vision, capture=backend)

        coarse_bounds = NormalizedVisualBounds(10, 200, 40, 240)
        vision.locate_control = lambda *_: VisualTargetResult(
            VisualTargetStatus.FOUND,
            "+",
            "Coarse candidate",
            coarse_bounds,
            0.90,
        )
        vision.refine_control = lambda *_: VisualTargetResult(status, reason=reason)

        result = registry.execute("visual_click", ARGS)
        assert not result.success
        assert "The visual target was not found or was ambiguous. No click prepared." in result.message
        assert f"Diagnostic: coarse=found, refine={status.value}: {reason[:-1]}." in result.message

        # Must not have pending confirmation
        assert not registry.has_pending_confirmation()
        # Must not move mouse or click (no fallback to coarse geometry!)
        assert not mouse.moves and mouse.clicks == 0
        # Exactly 1 capture and 2 provider calls
        assert len(capture.calls) == 1


def test_refinement_provider_failure_fail_closed():
    backend = FakeWindowCaptureBackend()
    vision = FakeVisualPerceptionProvider()
    registry, _, mouse, capture, _ = _harness(provider=vision, capture=backend)

    vision.locate_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.FOUND,
        "+",
        "Coarse candidate",
        NormalizedVisualBounds(10, 200, 40, 240),
        0.90,
    )
    def fail_refinement(*_):
        raise VisualPerceptionUnavailableError("timeout")
    vision.refine_control = fail_refinement

    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target could not be verified.\n"
        "Diagnostic: refinement provider failure."
    )
    assert not registry.has_pending_confirmation()
    assert not mouse.moves and mouse.clicks == 0


def test_visual_click_sensitive_and_visual_target_safe():
    registry, _, _, _, _ = _harness()
    prepared_click = registry.prepare("visual_click", ARGS)
    assert prepared_click.risk_level is RiskLevel.SENSITIVE

    prepared_target = registry.prepare("visual_target", ARGS)
    assert prepared_target.risk_level is RiskLevel.SAFE
