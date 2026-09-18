from __future__ import annotations

import logging

from desktop_assistant.intent.models import IntentKind
from desktop_assistant.intent.provider import IntentProvider, IntentProviderError
from desktop_assistant.models import RiskLevel, ToolResult
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import ToolRegistry


class Assistant:
    """Orchestrates deterministic routing and optional natural-language intent."""

    def __init__(
        self,
        router: CommandRouter,
        registry: ToolRegistry,
        intent_provider: IntentProvider | None = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._intent_provider = intent_provider
        self._logger = logging.getLogger(__name__)

    def handle(self, request: str) -> ToolResult:
        self._logger.info("Handling request", extra={"request_length": len(request)})
        deterministic = self._router.route_detailed(request)
        if deterministic.recognized or self._intent_provider is None:
            result = deterministic.result
        else:
            result = self._resolve_natural_language(request)
        self._logger.info(
            "Request completed",
            extra={"success": result.success, "risk_level": result.risk_level.value},
        )
        return result

    def _resolve_natural_language(self, request: str) -> ToolResult:
        try:
            intent = self._intent_provider.resolve(request)  # type: ignore[union-attr]
        except IntentProviderError:
            self._logger.warning("Natural-language intent routing is unavailable")
            return self._provider_failure()
        except Exception:
            self._logger.exception("Unexpected intent-provider failure")
            return self._provider_failure()

        if intent.kind is IntentKind.TOOL_ACTION and intent.action is not None:
            return self._registry.execute(intent.action.tool_name, intent.action.arguments)
        if intent.kind is IntentKind.CONVERSATION and intent.message:
            return ToolResult(True, intent.message, RiskLevel.SAFE)
        if intent.kind is IntentKind.UNSUPPORTED and intent.message:
            return ToolResult(False, intent.message, RiskLevel.SAFE)
        return self._provider_failure()

    @staticmethod
    def _provider_failure() -> ToolResult:
        return ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
