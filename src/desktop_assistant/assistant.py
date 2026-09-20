import logging
import threading
from typing import Any

from desktop_assistant.cancellation import CancellationToken
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
        self._shutting_down = False
        self._execution_lock = threading.Lock()

    def handle(
        self,
        request: str,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> AssistantResponse:
        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

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
            if deterministic.direct_result is not None:
                if self._is_cancelled(cancellation_token):
                    return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))
                return self._completed(deterministic.direct_result)

            if deterministic.action is not None:
                return self._execute_action(
                    deterministic.action.tool_name,
                    deterministic.action.arguments,
                    cancellation_token=cancellation_token,
                )

        if self._intent_provider is None:
            if self._is_cancelled(cancellation_token):
                return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))
            return self._completed(
                deterministic.fallback_result
                or ToolResult(
                    False,
                    "I did not understand that command. Type 'help' to see supported commands.",
                    RiskLevel.SAFE,
                )
            )

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

        if intent.kind is IntentKind.CONVERSATION:
            if self._is_cancelled(cancellation_token):
                return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))
            return self._completed(ToolResult(True, intent.message or "How can I help?", RiskLevel.SAFE))

        if intent.kind is IntentKind.TOOL_ACTION:
            if intent.action is None:
                return self._completed(ToolResult(False, "The requested action was invalid.", RiskLevel.SAFE))
            return self._execute_action(
                intent.action.tool_name,
                intent.action.arguments,
                cancellation_token=cancellation_token,
            )

        return self._completed(
            ToolResult(False, intent.message or "That action is not supported yet.", RiskLevel.SAFE)
        )

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

        with self._execution_lock:
            self._shutting_down = True
            self._tool_registry.discard_pending_confirmation()

    def _execute_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> AssistantResponse:
        with self._execution_lock:
            if self._is_cancelled(cancellation_token):
                logger.info("Tool action '%s' discarded before execution due to cancellation/shutdown", tool_name)
                return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))
            outcome = self._tool_registry.execute(tool_name, arguments)
            return self._response(outcome)

    def _is_cancelled(self, token: CancellationToken | None) -> bool:
        return self._shutting_down or (token is not None and token.is_cancelled)

    @staticmethod
    def _response(outcome: RegistryOutcome) -> AssistantResponse:
        if isinstance(outcome, ConfirmationRequest):
            return AssistantResponse.confirmation_required(outcome)
        return AssistantResponse.completed(outcome)

    @staticmethod
    def _completed(result: ToolResult) -> AssistantResponse:
        return AssistantResponse.completed(result)
