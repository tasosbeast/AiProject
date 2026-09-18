from __future__ import annotations

import logging

from desktop_assistant.models import ToolResult
from desktop_assistant.router import CommandRouter


class Assistant:
    """Application orchestrator; future intent providers can sit above the router."""

    def __init__(self, router: CommandRouter) -> None:
        self._router = router
        self._logger = logging.getLogger(__name__)

    def handle(self, request: str) -> ToolResult:
        self._logger.info("Handling request", extra={"request_length": len(request)})
        result = self._router.route(request)
        self._logger.info(
            "Request completed",
            extra={"success": result.success, "risk_level": result.risk_level.value},
        )
        return result

