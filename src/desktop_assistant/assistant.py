from __future__ import annotations

import logging

from desktop_assistant.intent.models import IntentKind, IntentResult
from desktop_assistant.intent.provider import IntentProvider, IntentProviderError
from desktop_assistant.models import (
    AssistantResponse,
    ConfirmationRequest,
    RiskLevel,
    ToolResult,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import RegistryOutcome, ToolRegistry


logger = logging.getLogger(__name__)


class Assistant:
    """Coordinates deterministic routing, optional intent resolution, and confirmation."""

    def __init__(
        self,
        router: CommandRouter,
        tool_registry: ToolRegistry,
        intent_provider: IntentProvider | None = None,
    ) -> None:
        self._router = router
        self._tool_registry = tool_registry
        self._intent_provider = intent_provider

    def handle(self, request: str) -> AssistantResponse:
        if self._tool_registry.has_pending_confirmation():
            return self._completed(
                ToolResult(
                    False,
                    "Confirm or cancel the pending action before starting another request.",
                    RiskLevel.SAFE,
                )
            )

        deterministic = self._router.route_detailed(request)
        if deterministic.recognized:
            return self._response(deterministic.result)

        if self._intent_provider is None:
            return self._completed(deterministic.result)

        try:
            intent = self._intent_provider.resolve(request)
        except IntentProviderError as exc:
            logger.warning("Intent provider unavailable: %s", type(exc).__name__)
            return self._completed(
                ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
            )
        except Exception:
            logger.exception("Unexpected intent provider failure")
            return self._completed(
                ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
            )

        return self._response(self._resolve_intent(intent))

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        """Approve the exact already-prepared action without re-routing it."""

        return self._completed(self._tool_registry.confirm(confirmation_id))

    def cancel(self, confirmation_id: str) -> AssistantResponse:
        """Cancel and discard one pending action without invoking a provider."""

        return self._completed(self._tool_registry.cancel(confirmation_id))

    def has_pending_confirmation(self) -> bool:
        return self._tool_registry.has_pending_confirmation()

    def shutdown(self) -> None:
        """Discard session-only authorization state during application shutdown."""

        self._tool_registry.discard_pending_confirmation()

    def _resolve_intent(self, intent: IntentResult) -> RegistryOutcome:
        if intent.kind is IntentKind.TOOL_ACTION:
            if intent.action is None:
                return ToolResult(False, "The requested action was invalid.", RiskLevel.SAFE)
            return self._tool_registry.execute(intent.action.tool_name, intent.action.arguments)
        if intent.kind is IntentKind.CONVERSATION:
            return ToolResult(True, intent.message or "How can I help?", RiskLevel.SAFE)
        return ToolResult(
            False,
            intent.message or "That action is not supported yet.",
            RiskLevel.SAFE,
        )

    @staticmethod
    def _response(outcome: RegistryOutcome) -> AssistantResponse:
        if isinstance(outcome, ConfirmationRequest):
            return AssistantResponse.confirmation_required(outcome)
        return AssistantResponse.completed(outcome)

    @staticmethod
    def _completed(result: ToolResult) -> AssistantResponse:
        return AssistantResponse.completed(result)
