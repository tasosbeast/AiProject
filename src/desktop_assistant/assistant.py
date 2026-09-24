from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from desktop_assistant.cancellation import CancellationToken
from desktop_assistant.confirmation import PreparedAction
from desktop_assistant.intent.models import ActionPlan, IntentKind
from desktop_assistant.intent.provider import IntentProvider, IntentProviderError
from desktop_assistant.models import (
    AssistantResponse,
    ConfirmationRequest,
    PlanContext,
    RiskLevel,
    ToolResult,
)
from desktop_assistant.router import CommandRouter
from desktop_assistant.tool_registry import RegistryOutcome, ToolRegistry


logger = logging.getLogger(__name__)

MAX_OBSERVATION_CHARS = 2000


def _format_observation(result: ToolResult) -> str:
    text = (result.message or "").strip()
    if len(text) > MAX_OBSERVATION_CHARS:
        suffix = "\n[Observation truncated]"
        return text[: max(0, MAX_OBSERVATION_CHARS - len(suffix))] + suffix
    return text


@dataclass(slots=True)
class _PendingPlan:
    plan_id: str
    actions: tuple[PreparedAction, ...]
    completed_results: list[ToolResult]
    current_step_index: int
    confirmation_id: str | None = None


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
        self._shutdown_event = threading.Event()
        self._state_lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._pending_plan: _PendingPlan | None = None

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def _sync_pending_plan(self) -> None:
        """Synchronize _pending_plan with authoritative tool registry confirmation state."""
        with self._state_lock:
            if self._pending_plan is not None:
                if self._pending_plan.confirmation_id is not None:
                    if not self._tool_registry.has_pending_confirmation(self._pending_plan.confirmation_id):
                        logger.info(
                            "Pending plan discarded because its confirmation expired or was removed",
                            extra={"plan_id": self._pending_plan.plan_id},
                        )
                        self._pending_plan = None

    def handle(
        self,
        request: str,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> AssistantResponse:
        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        self._sync_pending_plan()
        if self.has_pending_confirmation():
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

        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        if intent.kind is IntentKind.CONVERSATION:
            return self._completed(ToolResult(True, intent.message or "How can I help?", RiskLevel.SAFE))

        if intent.kind is IntentKind.TOOL_ACTION:
            if intent.action is None:
                return self._completed(ToolResult(False, "The requested action was invalid.", RiskLevel.SAFE))
            return self._execute_action(
                intent.action.tool_name,
                intent.action.arguments,
                cancellation_token=cancellation_token,
            )

        if intent.kind is IntentKind.ACTION_PLAN:
            if intent.plan is None:
                return self._completed(ToolResult(False, "The requested action plan was invalid.", RiskLevel.SAFE))
            return self._handle_action_plan(intent.plan, cancellation_token=cancellation_token)

        if intent.kind is IntentKind.OBSERVE_UI_THEN_DECIDE:
            query = intent.observe_ui.query if intent.observe_ui is not None else ""
            if not query:
                return self._completed(ToolResult(False, "Window query must not be empty.", RiskLevel.SAFE))
            return self._handle_observe_ui_then_decide(
                request,
                query,
                cancellation_token=cancellation_token,
            )

        return self._completed(
            ToolResult(False, intent.message or "That action is not supported yet.", RiskLevel.SAFE)
        )

    def confirm(self, confirmation_id: str) -> AssistantResponse:
        """Approve the exact already-prepared action without re-routing it."""
        if self._is_cancelled(None):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        with self._execution_lock:
            if self._is_cancelled(None):
                return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

            self._sync_pending_plan()
            with self._state_lock:
                pending_plan = self._pending_plan

            if pending_plan is not None and pending_plan.confirmation_id == confirmation_id:
                res = self._tool_registry.confirm(confirmation_id)
                if not res.success:
                    with self._state_lock:
                        self._pending_plan = None
                    pending_plan.completed_results.append(res)
                    total_steps = len(pending_plan.actions)
                    step_num = pending_plan.current_step_index + 1
                    msg = (
                        f"Plan stopped at step {step_num} of {total_steps}: {res.message} "
                        f"Remaining steps were not run."
                    )
                    return self._completed(
                        ToolResult(False, msg, self._aggregate_risk(pending_plan.completed_results))
                    )

                pending_plan.completed_results.append(res)
                total_steps = len(pending_plan.actions)

                # Check for shutdown after sensitive action finishes before starting remaining safe steps
                for step_idx in range(pending_plan.current_step_index + 1, total_steps):
                    if self._is_cancelled(None):
                        with self._state_lock:
                            self._pending_plan = None
                        msg = (
                            f"Plan cancelled at step {step_idx + 1} of {total_steps}. "
                            f"{len(pending_plan.completed_results)} action(s) completed before cancellation."
                        )
                        return self._completed(
                            ToolResult(False, msg, self._aggregate_risk(pending_plan.completed_results))
                        )

                    next_action = pending_plan.actions[step_idx]
                    step_outcome = self._tool_registry.dispatch_prepared(next_action)
                    if isinstance(step_outcome, ConfirmationRequest):
                        with self._state_lock:
                            self._pending_plan.current_step_index = step_idx
                            self._pending_plan.confirmation_id = step_outcome.confirmation_id
                        return AssistantResponse.confirmation_required(step_outcome)

                    if not step_outcome.success:
                        pending_plan.completed_results.append(step_outcome)
                        with self._state_lock:
                            self._pending_plan = None
                        msg = (
                            f"Plan stopped at step {step_idx + 1} of {total_steps}: {step_outcome.message} "
                            f"Remaining steps were not run."
                        )
                        return self._completed(
                            ToolResult(False, msg, self._aggregate_risk(pending_plan.completed_results))
                        )

                    pending_plan.completed_results.append(step_outcome)

                with self._state_lock:
                    self._pending_plan = None
                msg = self._format_plan_summary(pending_plan.completed_results)
                return self._completed(
                    ToolResult(True, msg, self._aggregate_risk(pending_plan.completed_results))
                )

            return self._completed(self._tool_registry.confirm(confirmation_id))

    def cancel(self, confirmation_id: str) -> AssistantResponse:
        """Cancel and discard one pending action without invoking a provider."""
        with self._execution_lock:
            self._sync_pending_plan()
            with self._state_lock:
                pending_plan = self._pending_plan

            if pending_plan is not None and pending_plan.confirmation_id == confirmation_id:
                cancel_res = self._tool_registry.cancel(confirmation_id)
                with self._state_lock:
                    self._pending_plan = None
                if not cancel_res.success:
                    return self._completed(cancel_res)

                step_num = pending_plan.current_step_index + 1
                total_steps = len(pending_plan.actions)
                completed_count = len(pending_plan.completed_results)
                if completed_count > 0:
                    msg = (
                        f"Plan cancelled at step {step_num} of {total_steps}. "
                        f"{completed_count} action(s) completed before cancellation. "
                        f"Remaining actions were discarded."
                    )
                else:
                    msg = (
                        f"Plan cancelled at step {step_num} of {total_steps}. "
                        f"Remaining actions were discarded."
                    )
                return self._completed(ToolResult(True, msg, RiskLevel.SAFE))

            return self._completed(self._tool_registry.cancel(confirmation_id))

    def has_pending_confirmation(self) -> bool:
        self._sync_pending_plan()
        with self._state_lock:
            return self._pending_plan is not None or self._tool_registry.has_pending_confirmation()

    def shutdown(self) -> None:
        """Signal shutdown immediately without blocking and discard session-only authorization state."""
        self._shutdown_event.set()
        with self._state_lock:
            self._pending_plan = None
        self._tool_registry.discard_pending_confirmation()

    def _handle_action_plan(
        self,
        plan: ActionPlan,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> AssistantResponse:
        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        actions = plan.actions
        if not (2 <= len(actions) <= 3):
            return self._completed(
                ToolResult(False, "Action plan must contain between 2 and 3 actions.", RiskLevel.SAFE)
            )

        prepared_actions: list[PreparedAction] = []
        for i, action in enumerate(actions, start=1):
            prepared = self._tool_registry.prepare(action.tool_name, action.arguments)
            if isinstance(prepared, ToolResult):
                return self._completed(
                    ToolResult(
                        False,
                        f"Action plan could not be prepared at step {i}: {prepared.message}",
                        RiskLevel.SAFE,
                    )
                )
            prepared_actions.append(prepared)

        if any(p.risk_level is RiskLevel.DESTRUCTIVE for p in prepared_actions):
            return self._completed(
                ToolResult(
                    False,
                    "Action plans cannot contain destructive actions.",
                    RiskLevel.SAFE,
                )
            )

        sensitive_count = sum(1 for p in prepared_actions if p.risk_level is RiskLevel.SENSITIVE)
        if sensitive_count > 1:
            return self._completed(
                ToolResult(
                    False,
                    "Action plans can contain at most one sensitive action requiring confirmation.",
                    RiskLevel.SAFE,
                )
            )

        with self._execution_lock:
            if self._is_cancelled(cancellation_token):
                return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

            completed_results: list[ToolResult] = []
            total_steps = len(prepared_actions)

            for i, prepared in enumerate(prepared_actions):
                if self._is_cancelled(cancellation_token):
                    msg = (
                        f"Plan cancelled at step {i + 1} of {total_steps}. "
                        f"{len(completed_results)} action(s) completed before cancellation."
                    )
                    return self._completed(
                        ToolResult(False, msg, self._aggregate_risk(completed_results))
                    )

                context = PlanContext(
                    step_index=i + 1,
                    total_steps=total_steps,
                    completed_summaries=tuple(r.message for r in completed_results),
                )
                outcome = self._tool_registry.dispatch_prepared(prepared, plan_context=context)

                if isinstance(outcome, ConfirmationRequest):
                    with self._state_lock:
                        self._pending_plan = _PendingPlan(
                            plan_id=uuid4().hex,
                            actions=tuple(prepared_actions),
                            completed_results=completed_results,
                            current_step_index=i,
                            confirmation_id=outcome.confirmation_id,
                        )
                    return AssistantResponse.confirmation_required(outcome)

                if not outcome.success:
                    completed_results.append(outcome)
                    msg = (
                        f"Plan stopped at step {i + 1} of {total_steps}: {outcome.message} "
                        f"Remaining steps were not run."
                    )
                    return self._completed(
                        ToolResult(False, msg, self._aggregate_risk(completed_results))
                    )

                completed_results.append(outcome)

            msg = self._format_plan_summary(completed_results)
            return self._completed(
                ToolResult(True, msg, self._aggregate_risk(completed_results))
            )

    def _handle_observe_ui_then_decide(
        self,
        request: str,
        query: str,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> AssistantResponse:
        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        # 1. Execute SAFE ui_inspect through ToolRegistry
        with self._execution_lock:
            if self._is_cancelled(cancellation_token):
                return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))
            inspect_outcome = self._tool_registry.execute("ui_inspect", {"query": query})

        if isinstance(inspect_outcome, ConfirmationRequest):
            return AssistantResponse.confirmation_required(inspect_outcome)

        if not inspect_outcome.success:
            # If inspection fails: stop safely. Do not make the second provider call.
            return self._completed(inspect_outcome)

        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        # Format and bound sanitized observation
        observation = _format_observation(inspect_outcome)

        if self._intent_provider is None or not hasattr(self._intent_provider, "decide_from_observation"):
            return self._completed(
                ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
            )

        # 2. Second provider call: choose the final action from observation
        try:
            decision = self._intent_provider.decide_from_observation(request, observation)
        except IntentProviderError as exc:
            logger.warning("Second decision provider unavailable: %s", type(exc).__name__)
            return self._completed(
                ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
            )
        except Exception:
            logger.exception("Unexpected second decision provider failure")
            return self._completed(
                ToolResult(False, "AI routing is temporarily unavailable.", RiskLevel.SAFE)
            )

        if self._is_cancelled(cancellation_token):
            return self._completed(ToolResult(False, "Request was cancelled.", RiskLevel.SAFE))

        # Validate that the second decision is strictly one ToolAction, Conversation, or Unsupported
        if decision.kind is IntentKind.CONVERSATION:
            return self._completed(ToolResult(True, decision.message or "How can I help?", RiskLevel.SAFE))

        if decision.kind is IntentKind.UNSUPPORTED:
            return self._completed(
                ToolResult(False, decision.message or "That action is not supported yet.", RiskLevel.SAFE)
            )

        if decision.kind is IntentKind.TOOL_ACTION:
            if decision.action is None:
                return self._completed(ToolResult(False, "The requested action was invalid.", RiskLevel.SAFE))
            if decision.action.tool_name in ("observe_ui_then_decide", "ui_inspect"):
                return self._completed(ToolResult(False, "Observation cannot be chained.", RiskLevel.SAFE))
            return self._execute_action(
                decision.action.tool_name,
                decision.action.arguments,
                cancellation_token=cancellation_token,
            )

        return self._completed(ToolResult(False, "Observation cannot be chained or planned.", RiskLevel.SAFE))

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
        return self._shutdown_event.is_set() or (token is not None and token.is_cancelled)

    @staticmethod
    def _format_plan_summary(results: list[ToolResult]) -> str:
        lines = [f"Completed {len(results)} actions:"]
        for i, result in enumerate(results, start=1):
            lines.append(f"{i}. {result.message}")
        return "\n".join(lines)

    @staticmethod
    def _aggregate_risk(results: list[ToolResult]) -> RiskLevel:
        if any(r.risk_level is RiskLevel.DESTRUCTIVE for r in results):
            return RiskLevel.DESTRUCTIVE
        if any(r.risk_level is RiskLevel.SENSITIVE for r in results):
            return RiskLevel.SENSITIVE
        return RiskLevel.SAFE

    @staticmethod
    def _response(outcome: RegistryOutcome) -> AssistantResponse:
        if isinstance(outcome, ConfirmationRequest):
            return AssistantResponse.confirmation_required(outcome)
        return AssistantResponse.completed(outcome)

    @staticmethod
    def _completed(result: ToolResult) -> AssistantResponse:
        return AssistantResponse.completed(result)
