import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import FakeWindowController, FakeWindowCaptureBackend, FakeVisualPerceptionProvider
from test_visual_perception import TARGET
from desktop_assistant.config import AppCatalog
from desktop_assistant.visual_perception import (
    NormalizedVisualBounds as Bounds, VisualTargetResult as Result, VisualTargetStatus as Status,
    VisualTargetTool, OpenAIVisualPerceptionProvider, _target_crop, _global_target_bounds,
)


def found(bounds=None):
    return Result(Status.FOUND, "Search", "Second icon", bounds or Bounds(24, 178, 53, 224), 0.99)


def run(coarse, refined):
    backend = FakeWindowCaptureBackend()
    controller = FakeWindowController([TARGET])
    provider = FakeVisualPerceptionProvider(target_result=coarse)
    def refine(png, target):
        provider.refinement_calls.append((png, target))
        if isinstance(refined, Exception):
            raise refined
        return refined
    provider.refine_target = refine
    tool = VisualTargetTool(controller, AppCatalog(), backend, provider)
    prepared = tool.prepare({"query": "VS Code", "target": "Search"})
    result = tool.execute(prepared.execution_value)
    assert len(backend.calls) == len(provider.target_calls) == 1
    assert controller.focused_handles == controller.restored_handles == []
    return result, backend, provider


def test_high_confidence_still_refines_same_image_and_exposes_only_final_geometry(caplog):
    result, backend, provider = run(found(), found(Bounds(100, 100, 200, 200)))
    assert result.success
    crop, box = _target_crop(backend.capture.png_bytes, 800, 600, found().bounds)
    assert provider.target_calls == [(backend.capture.png_bytes, "Search")]
    assert provider.refinement_calls == [(crop, "Search")]
    assert box == (0, 0, 384, 384)
    assert result.details["normalized_bounds"] == dict(left=48, top=64, right=96, bottom=128)
    assert set(result.details) == {"title", "requested_target", "resolved_label", "confidence",
                                   "normalized_bounds", "width", "height"}
    assert "384" not in str(result.details)
    assert base64.b64encode(crop).decode() not in str(result)
    assert "bounds" not in caplog.text


@pytest.mark.parametrize("status", [Status.NOT_FOUND, Status.AMBIGUOUS])
def test_coarse_nonfound_never_refines(status):
    result, _, provider = run(Result(status), found())
    assert not result.success and not provider.refinement_calls
    assert "normalized_bounds" not in result.details


def test_malformed_coarse_never_refines():
    coarse = found()
    object.__setattr__(coarse.bounds, "left", -1)
    result, _, provider = run(coarse, found(Bounds(100, 100, 200, 200)))
    assert not result.success and not provider.refinement_calls


@pytest.mark.parametrize("refined", [Result(Status.NOT_FOUND), Result(Status.AMBIGUOUS),
                                     RuntimeError("private provider error"), object()])
def test_refinement_failure_never_falls_back(refined):
    result, _, provider = run(found(), refined)
    assert not result.success and len(provider.refinement_calls) == 1
    assert "normalized_bounds" not in (result.details or {})
    assert "private provider error" not in str(result)


def test_invalid_refinement_and_collapsed_conversion_rejected():
    bad = found(Bounds(100, 100, 200, 200))
    object.__setattr__(bad.bounds, "right", 50)
    result, _, provider = run(found(), bad)
    assert not result.success and len(provider.refinement_calls) == 1
    with pytest.raises(ValueError):
        _global_target_bounds(Bounds(1, 1, 2, 2), (0, 0, 1, 1), 1600, 1200)


@pytest.mark.parametrize("bounds", [Bounds(0, 0, 1, 1), Bounds(999, 0, 1000, 1),
                                    Bounds(0, 999, 1, 1000), Bounds(999, 999, 1000, 1000),
                                    Bounds(500, 500, 501, 501)])
def test_crop_clamped_generous_deterministic_and_from_original_pixels(bounds):
    with Image.new("RGB", (800, 600)) as image, io.BytesIO() as output:
        image.putpixel((799, 599), (255, 0, 0))
        image.save(output, format="PNG")
        png = output.getvalue()
        crop, box = _target_crop(png, 800, 600, bounds)
        assert (crop, box) == _target_crop(png, 800, 600, bounds)
        left, top, right, bottom = box
        assert 0 <= left < right <= 800 and 0 <= top < bottom <= 600
        assert right - left >= 384 and bottom - top >= 384
        with Image.open(io.BytesIO(crop)) as actual, image.crop(box) as expected:
            assert actual.size == expected.size and actual.tobytes() == expected.tobytes()


def test_conversion_with_nonzero_offsets_and_half_up_rounding():
    assert _global_target_bounds(Bounds(100, 200, 900, 800), (200, 100, 600, 500), 800, 600) == Bounds(300, 300, 700, 700)
    assert _global_target_bounds(Bounds(1, 1, 999, 999), (0, 0, 400, 300), 800, 600) == Bounds(1, 1, 500, 500)


def test_refinement_provider_strict_crop_only_contract():
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(dict(status="not_found", label=None,
            description=None, bounds=None, confidence=None, reason="Not visible")))
    provider = OpenAIVisualPerceptionProvider(api_key="fake", model="fake",
        client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    provider.refine_target(b"crop", "Search")
    assert len(calls) == 1
    request = calls[0]
    assert request["store"] is False and "tools" not in request
    assert request["text"]["format"]["strict"] is True
    assert request["input"][0]["content"][0]["text"] == "Search"
    assert request["input"][0]["content"][1]["image_url"] == "data:image/png;base64,Y3JvcA=="
    instructions = request["instructions"]
    for phrase in ("refinement/verification", "Do not assume", "Nearby similar", "not_found",
                   "ambiguous", "Do not guess", "relative ONLY", "untrusted", "No action"):
        assert phrase in instructions


def test_small_image_crop_never_upscales():
    with Image.new("RGB", (90, 70)) as image, io.BytesIO() as output:
        image.save(output, format="PNG")
        crop, box = _target_crop(output.getvalue(), 90, 70, Bounds(0, 0, 1, 1))
    assert box == (0, 0, 90, 70)
    with Image.open(io.BytesIO(crop)) as image:
        assert image.size == (90, 70)


def test_targeting_transport_does_not_retry():
    import httpx
    from openai import OpenAI
    from desktop_assistant.visual_perception import VisualPerceptionUnavailableError
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"message": "rate limit"}})
    with httpx.Client(transport=httpx.MockTransport(fail)) as http_client:
        client = OpenAI(api_key="fake", http_client=http_client, max_retries=3)
        provider = OpenAIVisualPerceptionProvider(api_key="fake", model="fake", client=client)
        with pytest.raises(VisualPerceptionUnavailableError):
            provider.refine_target(b"crop", "Search")
    assert len(calls) == 1


def test_provider_image_and_target_limits():
    from desktop_assistant.visual_perception import (
        MAX_IMAGE_BYTES, MAX_TARGET_CHARS, MalformedVisualPerceptionResponseError,
    )
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text='{"status":"not_found","bounds":null}')
    provider = OpenAIVisualPerceptionProvider(api_key="fake", model="fake",
        client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(MalformedVisualPerceptionResponseError):
        provider.refine_target(b"x" * (MAX_IMAGE_BYTES + 1), "Search")
    assert not calls
    provider.refine_target(b"crop", "X" * 1000)
    assert len(calls[0]["input"][0]["content"][0]["text"]) == MAX_TARGET_CHARS
