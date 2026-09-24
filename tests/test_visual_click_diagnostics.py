from dataclasses import replace
import sys
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
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.process_control import WindowInfo
from desktop_assistant.visual_click import VisualClickTool, WindowRectangle
from desktop_assistant.visual_perception import (
    MalformedVisualPerceptionResponseError,
    NormalizedVisualBounds,
    VisualPerceptionUnavailableError,
    VisualProviderFailureCategory,
    VisualTargetResult,
    VisualTargetStatus,
    VisualTargetTool,
    _sanitize_diagnostic_reason,
)


ARGS = {"query": "VS Code", "target": "Search"}


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


def test_coarse_not_found_with_and_without_reason():
    # Coarse not found with reason
    provider = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(
            VisualTargetStatus.NOT_FOUND,
            reason="No + button found in tab bar.",
        )
    )
    registry, _, mouse, capture, _ = _harness(provider)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target was not found or was ambiguous. No click prepared.\n"
        "Diagnostic: coarse=not_found: No + button found in tab bar."
    )
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves
    assert len(capture.calls) == 1

    # Coarse not found without reason
    provider_no_reason = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(VisualTargetStatus.NOT_FOUND)
    )
    registry2, _, mouse2, _, _ = _harness(provider_no_reason)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == (
        "The visual target was not found or was ambiguous. No click prepared.\n"
        "Diagnostic: coarse=not_found."
    )
    assert not registry2.has_pending_confirmation()
    assert mouse2.clicks == 0 and not mouse2.moves


def test_coarse_ambiguous_with_and_without_reason():
    # Coarse ambiguous with reason
    provider = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(
            VisualTargetStatus.AMBIGUOUS,
            reason="Multiple + tabs match.",
        )
    )
    registry, _, mouse, _, _ = _harness(provider)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target was not found or was ambiguous. No click prepared.\n"
        "Diagnostic: coarse=ambiguous: Multiple + tabs match."
    )
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves

    # Coarse ambiguous without reason
    provider_no_reason = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(VisualTargetStatus.AMBIGUOUS)
    )
    registry2, _, mouse2, _, _ = _harness(provider_no_reason)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == (
        "The visual target was not found or was ambiguous. No click prepared.\n"
        "Diagnostic: coarse=ambiguous."
    )
    assert not registry2.has_pending_confirmation()
    assert mouse2.clicks == 0 and not mouse2.moves


def test_refinement_not_found_and_ambiguous_diagnostics():
    found_coarse = VisualTargetResult(
        VisualTargetStatus.FOUND,
        "Search",
        "Search control in toolbar",
        NormalizedVisualBounds(100, 100, 200, 200),
        0.95,
    )

    # Refinement not found with reason
    provider = FakeVisualPerceptionProvider()
    provider.locate_control = lambda *_: found_coarse
    provider.refine_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.NOT_FOUND,
        reason="Control not visible in crop.",
    )
    registry, _, mouse, capture, _ = _harness(provider)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target was not found or was ambiguous. No click prepared.\n"
        "Diagnostic: coarse=found, refine=not_found: Control not visible in crop."
    )
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves

    # Refinement ambiguous without reason
    provider.refine_control = lambda *_: VisualTargetResult(VisualTargetStatus.AMBIGUOUS)
    registry2, _, mouse2, _, _ = _harness(provider)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == (
        "The visual target was not found or was ambiguous. No click prepared.\n"
        "Diagnostic: coarse=found, refine=ambiguous."
    )
    assert not registry2.has_pending_confirmation()
    assert mouse2.clicks == 0 and not mouse2.moves


def test_provider_failure_diagnostics():
    # Coarse provider failure (unavailable error)
    provider_unavailable = FakeVisualPerceptionProvider()
    def raise_unavailable(*_):
        raise VisualPerceptionUnavailableError("timeout")
    provider_unavailable.locate_control = raise_unavailable
    registry, _, mouse, _, _ = _harness(provider_unavailable)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target could not be verified.\n"
        "Diagnostic: coarse provider failure."
    )
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves

    # Coarse provider failure (malformed response)
    provider_malformed = FakeVisualPerceptionProvider()
    def raise_malformed(*_):
        raise MalformedVisualPerceptionResponseError("bad json")
    provider_malformed.locate_control = raise_malformed
    registry2, _, mouse2, _, _ = _harness(provider_malformed)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == (
        "The visual target could not be verified.\n"
        "Diagnostic: coarse provider failure."
    )

    # Refinement provider failure
    provider_refine_fail = FakeVisualPerceptionProvider()
    provider_refine_fail.locate_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.FOUND,
        "Search",
        "Search control",
        NormalizedVisualBounds(100, 100, 200, 200),
        0.95,
    )
    def raise_refine_err(*_):
        raise MalformedVisualPerceptionResponseError("refinement error")
    provider_refine_fail.refine_control = raise_refine_err
    registry3, _, mouse3, _, _ = _harness(provider_refine_fail)
    result3 = registry3.execute("visual_click", ARGS)
    assert not result3.success
    assert result3.message == (
        "The visual target could not be verified.\n"
        "Diagnostic: refinement provider failure."
    )
    assert not registry3.has_pending_confirmation()
    assert mouse3.clicks == 0 and not mouse3.moves


def test_capture_and_stale_window_diagnostics():
    # Capture failure
    empty_capture = FakeWindowCaptureBackend(failed=True)
    registry, _, mouse, _, _ = _harness(capture=empty_capture)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target could not be verified.\n"
        "Diagnostic: capture failure."
    )
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves

    # Stale window before capture (window no longer valid in window controller)
    win_ctrl = FakeWindowController((TARGET,), active_window=TARGET)
    win_ctrl.valid_handles = set()  # Invalidate window
    registry2, _, mouse2, _, _ = _harness(windows=win_ctrl)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == (
        "The window changed during visual targeting. Request the click again.\n"
        "Diagnostic: stale window."
    )
    assert not registry2.has_pending_confirmation()
    assert mouse2.clicks == 0 and not mouse2.moves

    # Window rectangle changed during targeting
    mouse3 = FakeMouseClickController()
    provider3 = FakeVisualPerceptionProvider()
    orig_refine = provider3.refine_control
    def change_rect(*args):
        mouse3.rectangle = WindowRectangle(105, 205, 905, 805)
        return orig_refine(*args)
    provider3.refine_control = change_rect
    registry3, _, _, _, _ = _harness(provider=provider3, mouse=mouse3)
    result3 = registry3.execute("visual_click", ARGS)
    assert not result3.success
    assert result3.message == (
        "The window changed during visual targeting. Request the click again.\n"
        "Diagnostic: stale window."
    )
    assert not registry3.has_pending_confirmation()


def test_packaged_mode_remains_strictly_generic(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    # 1. Coarse not found
    provider = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(
            VisualTargetStatus.NOT_FOUND,
            reason="Private diagnostic detail",
        )
    )
    registry, _, mouse, _, _ = _harness(provider)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == "The visual target was not found or was ambiguous. No click prepared."
    assert "Diagnostic" not in result.message
    assert "Private diagnostic detail" not in result.message

    # 2. Refinement ambiguous
    provider2 = FakeVisualPerceptionProvider()
    provider2.locate_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.FOUND, "Search", "Search control",
        NormalizedVisualBounds(100, 100, 200, 200), 0.95,
    )
    provider2.refine_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.AMBIGUOUS, reason="Multiple matches in crop",
    )
    registry2, _, _, _, _ = _harness(provider2)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == "The visual target was not found or was ambiguous. No click prepared."
    assert "Diagnostic" not in result2.message

    # 3. Capture failure
    empty_capture = FakeWindowCaptureBackend(failed=True)
    registry3, _, _, _, _ = _harness(capture=empty_capture)
    result3 = registry3.execute("visual_click", ARGS)
    assert not result3.success
    assert result3.message == "The visual target could not be verified."
    assert "Diagnostic" not in result3.message

    # 4. Coarse provider failure
    provider4 = FakeVisualPerceptionProvider()
    provider4.locate_control = lambda *_: (_ for _ in ()).throw(VisualPerceptionUnavailableError("timeout"))
    registry4, _, _, _, _ = _harness(provider4)
    result4 = registry4.execute("visual_click", ARGS)
    assert not result4.success
    assert result4.message == "The visual target could not be verified."
    assert "Diagnostic" not in result4.message


def test_no_coordinate_bound_or_identifier_leaks():
    # Reason containing coordinates, bounds, HWND, raw JSON, base64
    polluted_reason = (
        '{"status": "not_found", "reason": "No match near bounds (120, 45, 180, 75) '
        'normalized_bounds: [12, 45, 18, 75] HWND 88776655 PID 99887766 '
        'base64 aGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgc3RyaW5nIQ=="}'
    )
    sanitized = _sanitize_diagnostic_reason(polluted_reason)
    for forbidden in (
        "88776655", "99887766", "120, 45", "180, 75",
        "normalized_bounds", "(120, 45, 180, 75)", "[12, 45, 18, 75]",
        "aGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgc3RyaW5nIQ==",
        '{"status"',
    ):
        assert forbidden not in sanitized

    # Execute with polluted reason and ensure final message is clean
    provider = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(
            VisualTargetStatus.NOT_FOUND,
            reason=polluted_reason,
        )
    )
    registry, _, _, _, _ = _harness(provider)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    for forbidden in (
        "88776655", "99887766", "120, 45", "180, 75",
        "normalized_bounds", "(120, 45, 180, 75)", "[12, 45, 18, 75]",
        "aGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgc3RyaW5nIQ==",
        '{"status"',
    ):
        assert forbidden not in result.message


def test_reason_sanitizer_edge_cases():
    assert _sanitize_diagnostic_reason(None) is None
    assert _sanitize_diagnostic_reason("") is None
    assert _sanitize_diagnostic_reason("   ") is None
    assert _sanitize_diagnostic_reason("A valid reason.") == "A valid reason"

    # Multi-line folded into one line
    multiline = "Line one.\nLine two\r\nLine three\twith tab"
    assert "\n" not in _sanitize_diagnostic_reason(multiline)
    assert "\r" not in _sanitize_diagnostic_reason(multiline)
    assert "\t" not in _sanitize_diagnostic_reason(multiline)

    # Length bounded
    long_reason = "word " * 100
    sanitized = _sanitize_diagnostic_reason(long_reason, limit=50)
    assert len(sanitized) <= 50
    assert sanitized.endswith("...")


def test_packaged_mode_stale_window(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    win_ctrl = FakeWindowController((TARGET,), active_window=TARGET)
    win_ctrl.valid_handles = set()
    registry, _, _, _, _ = _harness(windows=win_ctrl)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == "The window changed during visual targeting. Request the click again."
    assert "Diagnostic" not in result.message


def test_visual_target_outcome_sequence_protocol():
    from desktop_assistant.visual_perception import VisualTargetOutcome
    dummy_result = VisualTargetResult(VisualTargetStatus.FOUND, "btn", "desc", NormalizedVisualBounds(10, 10, 20, 20), 0.9)
    outcome = VisualTargetOutcome(result=dummy_result, width=640, height=480)
    res, w, h = outcome
    assert res is dummy_result
    assert w == 640
    assert h == 480
    assert outcome[0] is dummy_result
    assert outcome[1] == 640
    assert outcome[2] == 480
    assert len(outcome) == 3


def test_success_prepares_click_without_diagnostics():
    registry, _, mouse, capture, vision = _harness()
    result = registry.execute("visual_click", ARGS)
    from desktop_assistant.models import ConfirmationRequest
    assert isinstance(result, ConfirmationRequest)
    assert not registry.has_pending_confirmation() is False
    assert len(capture.calls) == 1
    assert len(vision.target_calls) == 1
    assert len(vision.refinement_calls) == 1


@pytest.mark.parametrize("category", [
    VisualProviderFailureCategory.TIMEOUT,
    VisualProviderFailureCategory.AUTHENTICATION,
    VisualProviderFailureCategory.RATE_LIMIT,
    VisualProviderFailureCategory.CONNECTION,
    VisualProviderFailureCategory.API_STATUS,
    VisualProviderFailureCategory.API,
    VisualProviderFailureCategory.EMPTY_RESPONSE,
    VisualProviderFailureCategory.MALFORMED_RESPONSE,
])
def test_coarse_provider_failure_categories(category: VisualProviderFailureCategory):
    provider = FakeVisualPerceptionProvider()
    if category in (VisualProviderFailureCategory.EMPTY_RESPONSE, VisualProviderFailureCategory.MALFORMED_RESPONSE):
        def raise_err(*_):
            raise MalformedVisualPerceptionResponseError("secret-coarse-error-token", category=category)
    else:
        def raise_err(*_):
            raise VisualPerceptionUnavailableError("secret-coarse-error-token", category=category)

    provider.locate_control = raise_err
    cap = FakeWindowCaptureBackend()
    registry, _, mouse, _, _ = _harness(provider=provider, capture=cap)
    result = registry.execute("visual_click", ARGS)

    assert not result.success
    expected_message = (
        "The visual target could not be verified.\n"
        f"Diagnostic: coarse provider failure: {category.value}."
    )
    assert result.message == expected_message
    assert "secret-coarse-error-token" not in result.message
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves
    assert len(cap.calls) == 1
    assert len(provider.refinement_calls) == 0
    assert len(provider.context_refinement_calls) == 0


@pytest.mark.parametrize("category", [
    VisualProviderFailureCategory.TIMEOUT,
    VisualProviderFailureCategory.AUTHENTICATION,
    VisualProviderFailureCategory.RATE_LIMIT,
    VisualProviderFailureCategory.CONNECTION,
    VisualProviderFailureCategory.API_STATUS,
    VisualProviderFailureCategory.API,
    VisualProviderFailureCategory.EMPTY_RESPONSE,
    VisualProviderFailureCategory.MALFORMED_RESPONSE,
])
def test_refinement_provider_failure_categories(category: VisualProviderFailureCategory):
    provider = FakeVisualPerceptionProvider(
        target_result=VisualTargetResult(
            VisualTargetStatus.FOUND,
            "Search",
            "Search control",
            NormalizedVisualBounds(100, 100, 200, 200),
            0.95,
        )
    )
    if category in (VisualProviderFailureCategory.EMPTY_RESPONSE, VisualProviderFailureCategory.MALFORMED_RESPONSE):
        def raise_err(*_):
            raise MalformedVisualPerceptionResponseError("secret-refine-error-token", category=category)
    else:
        def raise_err(*_):
            raise VisualPerceptionUnavailableError("secret-refine-error-token", category=category)

    provider.refine_control = raise_err
    cap = FakeWindowCaptureBackend()
    registry, _, mouse, _, _ = _harness(provider=provider, capture=cap)
    result = registry.execute("visual_click", ARGS)

    assert not result.success
    expected_message = (
        "The visual target could not be verified.\n"
        f"Diagnostic: refinement provider failure: {category.value}."
    )
    assert result.message == expected_message
    assert "secret-refine-error-token" not in result.message
    assert not registry.has_pending_confirmation()
    assert mouse.clicks == 0 and not mouse.moves
    assert len(cap.calls) == 1
    assert len(provider.target_calls) == 1


def test_packaged_mode_hides_failure_categories(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    # 1. Coarse failure with category
    provider_coarse = FakeVisualPerceptionProvider()
    provider_coarse.locate_control = lambda *_: (_ for _ in ()).throw(
        VisualPerceptionUnavailableError("bad connection", category=VisualProviderFailureCategory.CONNECTION)
    )
    registry1, _, _, _, _ = _harness(provider_coarse)
    result1 = registry1.execute("visual_click", ARGS)
    assert not result1.success
    assert result1.message == "The visual target could not be verified."
    assert "Diagnostic" not in result1.message
    assert "connection" not in result1.message

    # 2. Refinement failure with category
    provider_refine = FakeVisualPerceptionProvider()
    provider_refine.locate_control = lambda *_: VisualTargetResult(
        VisualTargetStatus.FOUND, "Search", "Search control",
        NormalizedVisualBounds(100, 100, 200, 200), 0.95,
    )
    provider_refine.refine_control = lambda *_: (_ for _ in ()).throw(
        MalformedVisualPerceptionResponseError("bad json", category=VisualProviderFailureCategory.MALFORMED_RESPONSE)
    )
    registry2, _, _, _, _ = _harness(provider_refine)
    result2 = registry2.execute("visual_click", ARGS)
    assert not result2.success
    assert result2.message == "The visual target could not be verified."
    assert "Diagnostic" not in result2.message
    assert "malformed_response" not in result2.message


def test_no_leaks_for_arbitrary_category_strings():
    provider = FakeVisualPerceptionProvider()
    # Pass an arbitrary string containing HWND, coords, or secrets
    polluted_category = "HWND=12345 bbox=(10,20,30,40) API_KEY=sk-abcdef"
    provider.locate_control = lambda *_: (_ for _ in ()).throw(
        VisualPerceptionUnavailableError("error msg", category=polluted_category)
    )
    registry, _, _, _, _ = _harness(provider)
    result = registry.execute("visual_click", ARGS)
    assert not result.success
    assert result.message == (
        "The visual target could not be verified.\n"
        "Diagnostic: coarse provider failure."
    )
    assert "12345" not in result.message
    assert "bbox" not in result.message
    assert "sk-abcdef" not in result.message
