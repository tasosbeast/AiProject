from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from time import monotonic
from uuid import uuid4

from desktop_assistant.models import (
    ConfirmationRequest,
    PlanContext,
    RiskLevel,
    ToolArguments,
    ToolResult,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """An exact, trusted action that can be executed without re-routing."""

    tool_name: str
    normalized_arguments: ToolArguments
    risk_level: RiskLevel
    summary: str
    warning: str
    execution_value: object = field(repr=False, compare=False)
    executor: Callable[[object], ToolResult] = field(repr=False, compare=False)
    registry_token: object = field(repr=False, compare=False)


class ConfirmationOutcome(str, Enum):
    APPROVED = "approved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConfirmationResolution:
    outcome: ConfirmationOutcome
    action: PreparedAction | None = None


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    confirmation_id: str
    action: PreparedAction
    created_at: float


class ConfirmationManager:
    """Stores one short-lived prepared action in memory for this session only."""

    def __init__(
        self,
        *,
        lifetime_seconds: float = 120.0,
        clock: Callable[[], float] = monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._lifetime_seconds = lifetime_seconds
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._pending: _PendingConfirmation | None = None
        self._lock = RLock()

    def has_pending(self) -> bool:
        with self._lock:
            self._expire_if_needed()
            return self._pending is not None

    def request(
        self,
        action: PreparedAction,
        plan_context: PlanContext | None = None,
    ) -> ConfirmationRequest | None:
        with self._lock:
            if self.has_pending():
                return None
            confirmation_id = self._id_factory()
            self._pending = _PendingConfirmation(confirmation_id, action, self._clock())
            logger.info(
                "Confirmation requested",
                extra={
                    "confirmation_id": self._correlation_id(confirmation_id),
                    "tool_name": action.tool_name,
                    "risk_level": action.risk_level.value,
                },
            )
            return ConfirmationRequest(
                confirmation_id=confirmation_id,
                summary=action.summary,
                risk_level=action.risk_level,
                warning=action.warning,
                plan_context=plan_context,
            )

    def approve(self, confirmation_id: str) -> ConfirmationResolution:
        with self._lock:
            if self._expire_if_needed():
                return ConfirmationResolution(ConfirmationOutcome.EXPIRED)
            pending = self._pending
            if pending is None or pending.confirmation_id != confirmation_id:
                return ConfirmationResolution(ConfirmationOutcome.UNKNOWN)
            self._pending = None
            logger.info(
                "Confirmation approved",
                extra={
                    "confirmation_id": self._correlation_id(confirmation_id),
                    "tool_name": pending.action.tool_name,
                    "risk_level": pending.action.risk_level.value,
                },
            )
            return ConfirmationResolution(ConfirmationOutcome.APPROVED, pending.action)

    def cancel(self, confirmation_id: str) -> ConfirmationResolution:
        with self._lock:
            if self._expire_if_needed():
                return ConfirmationResolution(ConfirmationOutcome.EXPIRED)
            pending = self._pending
            if pending is None or pending.confirmation_id != confirmation_id:
                return ConfirmationResolution(ConfirmationOutcome.UNKNOWN)
            self._pending = None
            logger.info(
                "Confirmation cancelled",
                extra={
                    "confirmation_id": self._correlation_id(confirmation_id),
                    "tool_name": pending.action.tool_name,
                    "risk_level": pending.action.risk_level.value,
                },
            )
            return ConfirmationResolution(ConfirmationOutcome.CANCELLED)

    def discard(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = None
            if pending is not None:
                logger.info(
                    "Pending confirmation discarded",
                    extra={
                        "confirmation_id": self._correlation_id(pending.confirmation_id),
                        "tool_name": pending.action.tool_name,
                        "risk_level": pending.action.risk_level.value,
                    },
                )

    def _expire_if_needed(self) -> bool:
        pending = self._pending
        if pending is None:
            return False
        if self._clock() - pending.created_at < self._lifetime_seconds:
            return False
        self._pending = None
        logger.info(
            "Confirmation expired",
            extra={
                "confirmation_id": self._correlation_id(pending.confirmation_id),
                "tool_name": pending.action.tool_name,
                "risk_level": pending.action.risk_level.value,
            },
        )
        return True

    @staticmethod
    def _correlation_id(confirmation_id: str) -> str:
        return confirmation_id[:8]
