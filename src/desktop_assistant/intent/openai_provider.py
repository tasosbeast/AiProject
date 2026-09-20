from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from desktop_assistant.intent.models import IntentKind, IntentResult
from desktop_assistant.intent.provider import (
    IntentProviderUnavailableError,
    MalformedIntentResponseError,
)


logger = logging.getLogger(__name__)

_INSTRUCTIONS = """You route one request for a small Windows desktop assistant.
When the user asks for 1 to 3 computer actions, propose 1 to 3 corresponding tool calls
in exact requested order. Never propose more than 3 actions. Never invent tools, shell
commands, executable paths, missing filesystem paths, arguments, or confirmation text.
Never propose loops, conditional branching (if/then), retries, or actions that depend
on the runtime output of a previous action.
Use respond_conversationally only for short questions about this assistant's identity
or current capabilities. Use report_unsupported for deletion, vague references such
as 'this file' without an exact path, broad requests, requests with more than 3 actions,
loops, conditional branching, or unsupported requests. Never mix conversation or
unsupported control calls with tool calls.
Never claim an action succeeded; local validation, safety policy, and confirmation
remain authoritative. Requests may be English, Greek, Greeklish, or mixed. For a bare
domain, use https://. Known-folder path values may start with Home, Desktop, Documents,
Downloads, Music, Pictures, or Videos. Preserve explicit source and destination paths."""

_CONTROL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": "respond_conversationally",
        "description": "Answer a short question about this assistant's identity or capabilities.",
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "report_unsupported",
        "description": "Return a concise limitation for an unsupported or unsafe request.",
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "strict": True,
    },
)


class OpenAIIntentProvider:
    """Stateless Responses API adapter. It proposes intents but executes nothing."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        tool_schemas: list[dict[str, Any]],
        timeout_seconds: float = 15.0,
        max_retries: int = 1,
        client: object | None = None,
    ) -> None:
        self._model = model
        self._tools = [*tool_schemas, *_CONTROL_SCHEMAS]
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def resolve(self, request: str) -> IntentResult:
        started = perf_counter()
        try:
            response = self._client.responses.create(  # type: ignore[attr-defined]
                model=self._model,
                instructions=_INSTRUCTIONS,
                input=request,
                tools=self._tools,
                tool_choice="required",
                parallel_tool_calls=True,
                max_output_tokens=256,
                store=False,
            )
        except APITimeoutError as exc:
            self._log_failure("timeout", started)
            raise IntentProviderUnavailableError("OpenAI request timed out.") from exc
        except AuthenticationError as exc:
            self._log_failure("authentication", started)
            raise IntentProviderUnavailableError("OpenAI authentication failed.") from exc
        except RateLimitError as exc:
            self._log_failure("rate_limit", started)
            raise IntentProviderUnavailableError("OpenAI rate limit reached.") from exc
        except APIConnectionError as exc:
            self._log_failure("connection", started)
            raise IntentProviderUnavailableError("OpenAI connection failed.") from exc
        except APIStatusError as exc:
            self._log_failure("api_status", started)
            raise IntentProviderUnavailableError("OpenAI API request failed.") from exc
        except APIError as exc:
            self._log_failure("api", started)
            raise IntentProviderUnavailableError("OpenAI API request failed.") from exc

        output = getattr(response, "output", None)
        if not isinstance(output, list):
            raise MalformedIntentResponseError("Response output was missing.")
        calls = [item for item in output if getattr(item, "type", None) == "function_call"]
        if not calls:
            raise MalformedIntentResponseError("Expected at least one structured intent.")

        control_names = {"respond_conversationally", "report_unsupported"}
        has_control = any(getattr(c, "name", None) in control_names for c in calls)
        has_tool = any(getattr(c, "name", None) not in control_names for c in calls)

        if has_control and has_tool:
            self._log_success(IntentKind.UNSUPPORTED, started)
            return IntentResult.unsupported("Mixed control and tool actions are not supported.")

        if has_control:
            if len(calls) > 1:
                self._log_success(IntentKind.UNSUPPORTED, started)
                return IntentResult.unsupported("Multiple control intents are not supported.")
            call = calls[0]
            name = getattr(call, "name", None)
            arguments = self._parse_arguments(getattr(call, "arguments", None))
            if name == "respond_conversationally":
                message = self._control_message(arguments)
                self._log_success(IntentKind.CONVERSATION, started)
                return IntentResult.conversation(message)
            if name == "report_unsupported":
                message = self._control_message(arguments)
                self._log_success(IntentKind.UNSUPPORTED, started)
                return IntentResult.unsupported(message)
            raise MalformedIntentResponseError(f"Unknown control call: {name}")

        # Only tool calls remain
        if len(calls) > 3:
            self._log_success(IntentKind.UNSUPPORTED, started)
            return IntentResult.unsupported("I can perform at most 3 actions in one plan.")

        from desktop_assistant.intent.models import ToolAction

        parsed_actions: list[ToolAction] = []
        for call in calls:
            name = getattr(call, "name", None)
            if not isinstance(name, str) or not name:
                raise MalformedIntentResponseError("Intent call name was missing.")
            arguments = self._parse_arguments(getattr(call, "arguments", None))
            parsed_actions.append(ToolAction(name, arguments))

        if len(parsed_actions) == 1:
            action = parsed_actions[0]
            logger.info(
                "Intent resolved",
                extra={
                    "model": self._model,
                    "result_type": IntentKind.TOOL_ACTION.value,
                    "requested_tool": action.tool_name,
                    "latency_ms": round((perf_counter() - started) * 1000),
                },
            )
            return IntentResult.tool_action(action.tool_name, action.arguments)

        logger.info(
            "Intent resolved",
            extra={
                "model": self._model,
                "result_type": IntentKind.ACTION_PLAN.value,
                "step_count": len(parsed_actions),
                "ordered_tools": [a.tool_name for a in parsed_actions],
                "latency_ms": round((perf_counter() - started) * 1000),
            },
        )
        return IntentResult.action_plan(tuple(parsed_actions))

    @staticmethod
    def _parse_arguments(raw_arguments: object) -> dict[str, Any]:
        if not isinstance(raw_arguments, str):
            raise MalformedIntentResponseError("Intent call arguments were missing.")
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MalformedIntentResponseError("Intent arguments were not valid JSON.") from exc
        if not isinstance(arguments, dict):
            raise MalformedIntentResponseError("Intent arguments must be an object.")
        return arguments

    @staticmethod
    def _control_message(arguments: dict[str, Any]) -> str:
        if set(arguments) != {"message"} or not isinstance(arguments.get("message"), str):
            raise MalformedIntentResponseError("Control intent contained invalid arguments.")
        message = arguments["message"].strip()
        if not message:
            raise MalformedIntentResponseError("Control intent message was empty.")
        return message

    def _log_failure(self, category: str, started: float) -> None:
        logger.warning(
            "Intent provider failed",
            extra={
                "model": self._model,
                "failure_category": category,
                "latency_ms": round((perf_counter() - started) * 1000),
            },
        )

    def _log_success(self, kind: IntentKind, started: float) -> None:
        logger.info(
            "Intent resolved",
            extra={
                "model": self._model,
                "result_type": kind.value,
                "latency_ms": round((perf_counter() - started) * 1000),
            },
        )
