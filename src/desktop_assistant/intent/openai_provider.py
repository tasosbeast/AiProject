from __future__ import annotations

from copy import deepcopy
import json
import logging
from time import perf_counter
from typing import Any, Sequence

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

_INSTRUCTIONS = """You route one user request for a small Windows desktop assistant.

SINGLE ACTION:
If the user requests exactly one supported computer action, call that individual tool.
For volume adjustments, use volume_control with action: volume_up, volume_down, or mute_toggle.
For media playback controls, use media_control with action: play_pause, next_track, or previous_track.
For system performance and hardware status, use system_status with metric: cpu, memory, battery, disk, or overview.
For opening trusted projects in VS Code, use open_project with project_name: AiProject.
For running predefined project tasks such as tests, use run_project_task with project_name: AiProject, task: tests.

For keyboard input into an explicitly named existing window, use window_input.
Use only its fixed action enum; never invent raw key codes or shortcut strings.
Preserve the exact requested text (including whitespace); use type_text_and_enter for text followed by Enter.
Text is limited to 2000 characters. Reject longer text rather than truncating it.
For non-text actions value is absent (null in the strict API transport).
Never substitute focus_window for a keyboard request. Never infer the target from 'here' or 'this window'.

MULTI ACTION:
If the user requests exactly 2 or 3 supported computer actions, you MUST call propose_action_plan exactly once and include EVERY requested action in exact requested order.
Never fulfill only the first part of a multi-action request.

Never propose more than 3 actions. Never invent tools, shell commands, executable paths, missing filesystem paths, arguments, or confirmation text.
Never propose loops, conditional branching (if/then), retries, or actions that depend on the runtime output of a previous action.

Use respond_conversationally only for short questions about this assistant's identity or current capabilities.
Use report_unsupported for deletion, vague references such as 'this file' without an exact path, broad requests, requests with more than 3 actions, loops, conditional branching, or unsupported requests.
Never mix conversation or unsupported control calls with tool calls.

Examples:
User: 'Άνοιξε το Chrome και το Spotify.'
Action: propose_action_plan with 1. open_app Chrome, 2. open_app Spotify

User: 'Άνοιξε το Chrome, το Spotify και το Notepad.'
Action: propose_action_plan with 1. open_app Chrome, 2. open_app Spotify, 3. open_app Notepad

User: 'Άνοιξε Chrome και μετά open Spotify.'
Action: propose_action_plan with 1. open_app Chrome, 2. open_app Spotify

User: 'Open Chrome and Spotify.'
Action: propose_action_plan with 1. open_app Chrome, 2. open_app Spotify

User: 'Anoikse Chrome kai Spotify.'
Action: propose_action_plan with 1. open_app Chrome, 2. open_app Spotify

User: 'Άνοιξε το Spotify και βάλε μουσική.'
Action: propose_action_plan with 1. open_app Spotify, 2. media_control play_pause

User: 'Πες μου CPU και RAM.'
Action: propose_action_plan with 1. system_status metric: cpu, 2. system_status metric: memory

User: 'Άνοιξε το AiProject στο VS Code και τρέξε τα tests.'
Action: propose_action_plan with 1. open_project project_name: AiProject, 2. run_project_task project_name: AiProject, task: tests

User: 'Open AiProject in VS Code and run tests.'
Action: propose_action_plan with 1. open_project project_name: AiProject, 2. run_project_task project_name: AiProject, task: tests

User: 'Anoikse to AiProject sto VS Code kai trekse ta tests.'
Action: propose_action_plan with 1. open_project project_name: AiProject, 2. run_project_task project_name: AiProject, task: tests

User: 'Άνοιξε το AiProject στο VS Code.'
Action: open_project with project_name: AiProject

User: 'Τρέξε τα tests του AiProject.'
Action: run_project_task with project_name: AiProject, task: tests

User: 'Open AiProject in VS Code.'
Action: open_project with project_name: AiProject

User: 'Run AiProject tests.'
Action: run_project_task with project_name: AiProject, task: tests

User: 'Άνοιξε το Chrome.'
Action: open_app Chrome

User: 'Δυνάμωσε τη φωνή.'
Action: volume_control with action: volume_up

User: 'Turn down the volume.'
Action: volume_control with action: volume_down

User: 'Mute the audio.'
Action: volume_control with action: mute_toggle

User: 'Play music.' or 'Pause.'
Action: media_control with action: play_pause

User: 'Next track.'
Action: media_control with action: next_track

User: 'Previous song.'
Action: media_control with action: previous_track

User: 'Πόση RAM χρησιμοποιώ;' or 'How much RAM am I using?'
Action: system_status with metric: memory

User: 'Πόσο CPU χρησιμοποιείται;'
Action: system_status with metric: cpu

User: 'Πόση μπαταρία έχω;' or 'Check my battery.'
Action: system_status with metric: battery

User: 'Πόσο χώρο έχω στον δίσκο;'
Action: system_status with metric: disk

User: 'Πώς πάει ο υπολογιστής μου;'
Action: system_status with metric: overview

User: 'Τι παράθυρα είναι ανοιχτά;' or 'List open windows.' or 'Ti parathyra einai anoichta;'
Action: window_info with action: list

User: 'Σε ποιο παράθυρο είμαι;' or "What's the active window?" or 'Se poio parathyro eimai;'
Action: window_info with action: active

User: 'Πήγαινε στο VS Code.' or 'Switch to VS Code.' or 'Pygaine sto VS Code.'
Action: focus_window with query: VS Code

User: 'Φέρε μπροστά το Spotify.' or 'Bring Spotify to foreground.' or 'Fere mprosta to Spotify.'
Action: focus_window with query: Spotify

User: 'Πήγαινε στο παράθυρο Bookish.' or 'Focus Bookish.'
Action: focus_window with query: Bookish

User: 'Switch to Chrome.' or 'Πήγαινε στο Chrome.'
Action: focus_window with query: Chrome

User: 'Γράψε hello world στο Notepad.'
Action: window_input with query: Notepad, action: type_text, value: hello world

User: 'Γράψε hello και πάτα Enter στο Notepad.'
Action: window_input with query: Notepad, action: type_text_and_enter, value: hello

User: 'Πάτα Ctrl+S στο VS Code.'
Action: window_input with query: VS Code, action: ctrl_s

User: 'Πάτα Enter στο Bookish.'
Action: window_input with query: Bookish, action: enter

User: 'Press Ctrl+L in Chrome.'
Action: window_input with query: Chrome, action: ctrl_l

User: 'Write hello in Notepad.' or 'Grapse hello sto Notepad.'
Action: window_input with query: Notepad, action: type_text, value: hello

User: 'Pata Ctrl+S sto VS Code.' or 'Πάτα Ctrl+S in VS Code.'
Action: window_input with query: VS Code, action: ctrl_s

User: 'Τι controls έχει το Notepad;'
Action: ui_inspect with query: Notepad

User: 'Τι κουμπιά έχει το Chrome;'
Action: ui_inspect with query: Chrome

User: 'Δείξε μου τι υπάρχει στο παράθυρο Bookish.'
Action: ui_inspect with query: Bookish

User: 'Show me the controls in VS Code.'
Action: ui_inspect with query: VS Code

User: 'Ti koumpia exei to Notepad?'
Action: ui_inspect with query: Notepad

For ui_inspect, require an explicit target window or application name. Do not infer 'this window' or 'here'.

User: 'Πάτα Settings στο Notepad.' or 'Press Settings in Notepad.' or 'Pata Settings sto Notepad.'
Action: ui_action with query: Notepad, control: Settings, action: invoke

User: 'Άνοιξε το File menu στο Notepad.' or 'Expand File in Notepad.' or 'Anoikse to File menu sto Notepad.'
Action: ui_action with query: Notepad, control: File, action: expand

User: 'Πήγαινε στο tab Untitled στο Notepad.' or 'Select tab Untitled in Notepad.'
Action: ui_action with query: Notepad, control: Untitled, action: select

User: 'Κλείσε το File menu στο Notepad.' or 'Collapse File in Notepad.'
Action: ui_action with query: Notepad, control: File, action: collapse

User: 'Κλείσε το Spell check στο Notepad.' or 'Turn off Spell check in Notepad.' or 'Kleise to Spell check sto Notepad.'
Action: ui_action with query: Notepad, control: Spell check, action: toggle_off

User: 'Άνοιξε το Spell check στο Notepad.' or 'Turn on Spell check in Notepad.' or 'Anoikse to Spell check sto Notepad.'
Action: ui_action with query: Notepad, control: Spell check, action: toggle_on

For ui_action, require an explicit target window, accessible control name, and action ('invoke', 'select', 'expand', 'collapse', 'toggle_on', 'toggle_off').

User: 'Άνοιξε το Spotify και μετά γύρνα στο VS Code.'
Action: propose_action_plan with 1. open_app Spotify, 2. focus_window query: VS Code

User: 'Open Spotify and switch to VS Code.'
Action: propose_action_plan with 1. open_app Spotify, 2. focus_window query: VS Code

User: 'Anoikse Spotify kai meta gyrna sto VS Code.'
Action: propose_action_plan with 1. open_app Spotify, 2. focus_window query: VS Code

Never claim an action succeeded; local validation, safety policy, and confirmation remain authoritative. Requests may be English, Greek, Greeklish, or mixed. For a bare domain, use https://. Known-folder path values may start with Home, Desktop, Documents, Downloads, Music, Pictures, or Videos. Preserve explicit source and destination paths."""

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


def build_plan_tool_schema(tool_schemas: Sequence[dict[str, Any]]) -> dict[str, Any]:
    action_variants: list[dict[str, Any]] = []
    for tool in tool_schemas:
        parameters = deepcopy(tool.get("parameters", {}))
        if not isinstance(parameters, dict):
            parameters = {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            }
        action_variants.append(
            {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "enum": [tool["name"]],
                    },
                    "arguments": parameters,
                },
                "required": ["tool_name", "arguments"],
                "additionalProperties": False,
            }
        )

    items_schema: dict[str, Any]
    if action_variants:
        items_schema = {"anyOf": action_variants}
    else:
        items_schema = {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "arguments": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            "required": ["tool_name", "arguments"],
            "additionalProperties": False,
        }

    return {
        "type": "function",
        "name": "propose_action_plan",
        "description": "Propose an ordered sequential plan of 2 to 3 computer actions when the user requests 2 or 3 actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": items_schema,
                },
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
        "strict": True,
    }


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
        base_tool_schemas = [
            t for t in tool_schemas if t.get("name") != "propose_action_plan"
        ]
        self._registered_tools = {tool["name"]: tool for tool in base_tool_schemas if "name" in tool}
        wire_schemas = deepcopy(base_tool_schemas)
        for tool in wire_schemas:
            parameters = tool.get("parameters", {})
            required = parameters.get("required", [])
            for name, prop in parameters.get("properties", {}).items():
                if name not in required:
                    prop["type"] = [prop["type"], "null"]
                    if "enum" in prop:
                        prop["enum"].append(None)
            parameters["required"] = list(parameters.get("properties", {}))
        self._plan_schema = build_plan_tool_schema(wire_schemas)
        self._tools = [*wire_schemas, self._plan_schema, *_CONTROL_SCHEMAS]
        self._client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    @property
    def plan_schema(self) -> dict[str, Any]:
        return deepcopy(self._plan_schema)

    def resolve(self, request: str) -> IntentResult:
        started = perf_counter()
        try:
            response = self._client.responses.create(  # type: ignore[attr-defined]
                model=self._model,
                instructions=_INSTRUCTIONS,
                input=request,
                tools=self._tools,
                tool_choice="required",
                parallel_tool_calls=False,
                max_output_tokens=8192,
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

        if len(calls) > 1:
            self._log_success(IntentKind.UNSUPPORTED, started)
            return IntentResult.unsupported("Multiple independent function calls are not supported.")

        call = calls[0]
        name = getattr(call, "name", None)
        if not isinstance(name, str) or not name:
            raise MalformedIntentResponseError("Intent call name was missing.")

        arguments = self._parse_arguments(getattr(call, "arguments", None))

        if name == "respond_conversationally":
            message = self._control_message(arguments)
            self._log_success(IntentKind.CONVERSATION, started)
            return IntentResult.conversation(message)

        if name == "report_unsupported":
            message = self._control_message(arguments)
            self._log_success(IntentKind.UNSUPPORTED, started)
            return IntentResult.unsupported(message)

        if name == "propose_action_plan":
            return self._parse_action_plan(arguments, started)

        # Single registered tool call
        from desktop_assistant.intent.models import ToolAction

        if self._registered_tools and name not in self._registered_tools:
            raise MalformedIntentResponseError(f"Unknown tool call: {name}")

        action = ToolAction(name, self._local_arguments(name, arguments))
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

    def _parse_action_plan(self, arguments: dict[str, Any], started: float) -> IntentResult:
        if not isinstance(arguments, dict):
            raise MalformedIntentResponseError("propose_action_plan arguments must be an object.")
        raw_actions = arguments.get("actions")
        if not isinstance(raw_actions, list):
            raise MalformedIntentResponseError("propose_action_plan 'actions' must be a list.")

        if len(raw_actions) > 3:
            self._log_success(IntentKind.UNSUPPORTED, started)
            return IntentResult.unsupported("I can perform at most 3 actions in one plan.")
        if len(raw_actions) < 2:
            self._log_success(IntentKind.UNSUPPORTED, started)
            return IntentResult.unsupported("Plans must contain between 2 and 3 actions.")

        from desktop_assistant.intent.models import ToolAction

        parsed_actions: list[ToolAction] = []
        for idx, item in enumerate(raw_actions):
            if not isinstance(item, dict):
                raise MalformedIntentResponseError(f"Plan action {idx + 1} must be an object.")
            tool_name = item.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                raise MalformedIntentResponseError(f"Plan action {idx + 1} tool_name was missing.")
            item_arguments = item.get("arguments")
            if not isinstance(item_arguments, dict):
                raise MalformedIntentResponseError(f"Plan action {idx + 1} arguments must be an object.")

            if self._registered_tools:
                if tool_name not in self._registered_tools:
                    raise MalformedIntentResponseError(f"Unknown plan tool: {tool_name}")
                tool_def = self._registered_tools[tool_name]
                expected_params = tool_def.get("parameters", {})
                required_props = expected_params.get("required", [])
                for req in required_props:
                    if req not in item_arguments:
                        raise MalformedIntentResponseError(
                            f"Plan tool '{tool_name}' missing required argument '{req}'."
                        )
                if not expected_params.get("additionalProperties", True):
                    allowed = set(expected_params.get("properties", {}).keys())
                    for k in item_arguments:
                        if k not in allowed:
                            raise MalformedIntentResponseError(
                                f"Plan tool '{tool_name}' has unexpected argument '{k}'."
                            )

            parsed_actions.append(ToolAction(tool_name, self._local_arguments(tool_name, item_arguments)))

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

    def _local_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        parameters = self._registered_tools.get(name, {}).get("parameters", {})
        optional = set(parameters.get("properties", {})) - set(parameters.get("required", []))
        # null represents omission only for explicitly optional fields in the wire schema.
        return {key: value for key, value in arguments.items() if value is not None or key not in optional}

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
