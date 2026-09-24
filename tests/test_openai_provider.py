from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import desktop_assistant.intent.openai_provider as provider_module
from desktop_assistant.intent.models import IntentKind
from desktop_assistant.intent.openai_provider import (
    OpenAIIntentProvider,
    build_plan_tool_schema,
)
from desktop_assistant.intent.provider import (
    IntentProviderUnavailableError,
    MalformedIntentResponseError,
)


class FakeResponses:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.responses = FakeResponses(response, error)


def function_call(name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, arguments=arguments)


TEST_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "open_app",
        "description": "Open an application.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "App name"}
            },
            "required": ["app_name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "open_folder",
        "description": "Open a folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path"}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_folder",
        "description": "List folder contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path"}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "volume_control",
        "description": "Control system volume.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Volume action",
                    "enum": ["volume_up", "volume_down", "mute_toggle"],
                }
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "media_control",
        "description": "Control media playback.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Media action",
                    "enum": ["play_pause", "next_track", "previous_track"],
                }
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "system_status",
        "description": "Check system performance metrics.",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "System metric",
                    "enum": ["cpu", "memory", "battery", "disk", "overview"],
                }
            },
            "required": ["metric"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "open_project",
        "description": "Open one trusted project in VS Code.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name of the trusted project.",
                    "enum": ["AiProject"],
                }
            },
            "required": ["project_name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "run_project_task",
        "description": "Run a predefined trusted task for a project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name of the trusted project.",
                    "enum": ["AiProject"],
                },
                "task": {
                    "type": "string",
                    "description": "Predefined task to run.",
                    "enum": ["tests"],
                },
            },
            "required": ["project_name", "task"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "window_info",
        "description": "Inspect visible top-level windows or identify the active foreground window.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to perform ('list' or 'active').",
                    "enum": ["list", "active"],
                }
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "focus_window",
        "description": "Bring an existing visible window to the foreground by name, title, or application.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Title, name, or application of the window to focus.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "ui_inspect",
        "description": "Inspect supported controls in one existing window.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Explicit window."}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "ui_action",
        "description": "Perform a confirmed UI Automation action on an existing window control.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Explicit window."},
                "control": {"type": "string", "description": "Control name."},
                "action": {
                    "type": "string",
                    "description": "UI action",
                    "enum": ["invoke", "select", "expand", "collapse"],
                },
            },
            "required": ["query", "control", "action"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def make_provider(
    client: FakeClient,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> OpenAIIntentProvider:
    return OpenAIIntentProvider(
        api_key="test-key",
        model="test-model",
        tool_schemas=TEST_TOOL_SCHEMAS if tool_schemas is None else tool_schemas,
        client=client,
    )


@pytest.mark.parametrize(("utterance", "query"), [
    ("Τι controls έχει το Notepad;", "Notepad"),
    ("Τι κουμπιά έχει το Chrome;", "Chrome"),
    ("Δείξε μου τι υπάρχει στο παράθυρο Bookish.", "Bookish"),
    ("Show me the controls in VS Code.", "VS Code"),
    ("Ti koumpia exei to Notepad?", "Notepad"),
])
def test_provider_accepts_ui_inspect_routing_for_explicit_targets(utterance, query):
    client = FakeClient(SimpleNamespace(output=[function_call(
        "ui_inspect", f'{{"query":"{query}"}}',
    )]))
    result = make_provider(client).resolve(utterance)
    assert result.action.tool_name == "ui_inspect"
    assert result.action.arguments == {"query": query}


@pytest.mark.parametrize(("utterance", "query", "control", "action"), [
    ("Πάτα Settings στο Notepad.", "Notepad", "Settings", "invoke"),
    ("Άνοιξε το File menu στο Notepad.", "Notepad", "File", "expand"),
    ("Πήγαινε στο tab Untitled στο Notepad.", "Notepad", "Untitled", "select"),
    ("Press Settings in Notepad.", "Notepad", "Settings", "invoke"),
    ("Pata Settings sto Notepad.", "Notepad", "Settings", "invoke"),
    ("Expand File in Notepad.", "Notepad", "File", "expand"),
    ("Collapse File in Notepad.", "Notepad", "File", "collapse"),
    ("Κλείσε το Spell check στο Notepad.", "Notepad", "Spell check", "toggle_off"),
    ("Άνοιξε το Spell check στο Notepad.", "Notepad", "Spell check", "toggle_on"),
    ("Turn off Spell check in Notepad.", "Notepad", "Spell check", "toggle_off"),
    ("Turn on Spell check in Notepad.", "Notepad", "Spell check", "toggle_on"),
    ("Kleise to Spell check sto Notepad.", "Notepad", "Spell check", "toggle_off"),
    ("Anoikse to Spell check sto Notepad.", "Notepad", "Spell check", "toggle_on"),
])
def test_provider_accepts_ui_action_routing(utterance, query, control, action):
    client = FakeClient(SimpleNamespace(output=[function_call(
        "ui_action", f'{{"query":"{query}","control":"{control}","action":"{action}"}}',
    )]))
    result = make_provider(client).resolve(utterance)
    assert result.action.tool_name == "ui_action"
    assert result.action.arguments == {"query": query, "control": control, "action": action}
    assert client.responses.calls[0]["input"] == utterance


def test_provider_uses_stateless_responses_api_with_single_call_settings() -> None:
    client = FakeClient(SimpleNamespace(output=[function_call("open_app", '{"app_name":"Spotify"}')]))

    result = make_provider(client).resolve("Please open Spotify")

    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "open_app"
    assert result.action.arguments == {"app_name": "Spotify"}
    request = client.responses.calls[0]
    assert request["model"] == "test-model"
    assert request["store"] is False
    assert request["parallel_tool_calls"] is False
    assert request["tool_choice"] == "required"


def test_provider_maps_control_intents_to_application_models() -> None:
    conversation = make_provider(
        FakeClient(
            SimpleNamespace(
                output=[
                    function_call(
                        "respond_conversationally",
                        '{"message":"I can open approved apps."}',
                    )
                ]
            )
        )
    ).resolve("What can you do?")
    unsupported = make_provider(
        FakeClient(
            SimpleNamespace(
                output=[function_call("report_unsupported", '{"message":"I cannot do that."}')]
            )
        )
    ).resolve("Delete files")

    assert conversation.kind is IntentKind.CONVERSATION
    assert conversation.message == "I can open approved apps."
    assert unsupported.kind is IntentKind.UNSUPPORTED
    assert unsupported.message == "I cannot do that."


def test_propose_action_plan_with_two_actions_yields_action_plan() -> None:
    two_actions = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":[{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"open_app","arguments":{"app_name":"Chrome"}}]}',
            )
        ]
    )
    result = make_provider(FakeClient(two_actions)).resolve("Open Spotify and Chrome")
    assert result.kind is IntentKind.ACTION_PLAN
    assert result.plan is not None
    assert len(result.plan.actions) == 2
    assert result.plan.actions[0].tool_name == "open_app"
    assert result.plan.actions[0].arguments == {"app_name": "Spotify"}
    assert result.plan.actions[1].tool_name == "open_app"
    assert result.plan.actions[1].arguments == {"app_name": "Chrome"}


def test_propose_action_plan_with_three_actions_preserves_order() -> None:
    three_actions = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Chrome"}},'
                '{"tool_name":"open_folder","arguments":{"path":"Downloads"}},'
                '{"tool_name":"list_folder","arguments":{"path":"Downloads"}}'
                ']}',
            )
        ]
    )
    result = make_provider(FakeClient(three_actions)).resolve(
        "Άνοιξε το Chrome, άνοιξε τα Downloads και δείξε μου τα αρχεία"
    )
    assert result.kind is IntentKind.ACTION_PLAN
    assert result.plan is not None
    assert len(result.plan.actions) == 3
    assert [a.tool_name for a in result.plan.actions] == [
        "open_app",
        "open_folder",
        "list_folder",
    ]
    assert [dict(a.arguments) for a in result.plan.actions] == [
        {"app_name": "Chrome"},
        {"path": "Downloads"},
        {"path": "Downloads"},
    ]


@pytest.mark.parametrize(
    "plan_payload",
    (
        '{"actions": "not-a-list"}',
        '{"actions": [{"tool_name": "open_app"}, {"tool_name": "open_app"}]}',
        '{"actions": [{"arguments": {}}, {"arguments": {}}]}',
        '{"actions": [1, 2]}',
        '{"actions": [{"tool_name": "open_app", "arguments": "not-a-dict"}, {"tool_name": "open_app", "arguments": {}}]}',
        '{"actions": [{"tool_name": "", "arguments": {"app_name": "Chrome"}}, {"tool_name": "open_app", "arguments": {"app_name": "Chrome"}}]}',
    ),
)
def test_malformed_plan_returns_no_partial_plan(plan_payload: str) -> None:
    malformed = SimpleNamespace(
        output=[function_call("propose_action_plan", plan_payload)]
    )
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(malformed)).resolve("Malformed plan")


def test_invalid_second_step_rejects_whole_plan() -> None:
    invalid_second = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"open_app","arguments":{"invalid_field":"Chrome"}}'
                ']}',
            )
        ]
    )
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(invalid_second)).resolve("Open Spotify and bad step")


def test_more_than_three_plan_steps_are_unsupported() -> None:
    four_actions = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Chrome"}},'
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"open_app","arguments":{"app_name":"VS Code"}},'
                '{"tool_name":"open_app","arguments":{"app_name":"Notepad"}}'
                ']}',
            )
        ]
    )
    result = make_provider(FakeClient(four_actions)).resolve("Open four apps")
    assert result.kind is IntentKind.UNSUPPORTED
    assert "at most 3 actions" in (result.message or "")


def test_fewer_than_two_plan_steps_are_rejected() -> None:
    one_action = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":[{"tool_name":"open_app","arguments":{"app_name":"Chrome"}}]}',
            )
        ]
    )
    result = make_provider(FakeClient(one_action)).resolve("Open one in plan")
    assert result.kind is IntentKind.UNSUPPORTED
    assert "between 2 and 3 actions" in (result.message or "")


def test_unknown_tool_discriminator_rejected() -> None:
    unknown_tool = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Chrome"}},'
                '{"tool_name":"format_c_drive","arguments":{}}'
                ']}',
            )
        ]
    )
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(unknown_tool)).resolve("Open and format")


def test_wrong_arguments_for_discriminator_rejected() -> None:
    wrong_args = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"path":"Downloads"}},'
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}}'
                ']}',
            )
        ]
    )
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(wrong_args)).resolve("Open apps with wrong argument")


def test_multiple_top_level_function_calls_executes_nothing() -> None:
    multi_call = SimpleNamespace(
        output=[
            function_call("open_app", '{"app_name":"Chrome"}'),
            function_call("open_app", '{"app_name":"Spotify"}'),
        ]
    )
    result = make_provider(FakeClient(multi_call)).resolve("Open both")
    assert result.kind is IntentKind.UNSUPPORTED
    assert result.action is None
    assert result.plan is None
    assert "Multiple independent function calls are not supported." in (result.message or "")


def test_plan_tool_schema_structure() -> None:
    schema = build_plan_tool_schema(TEST_TOOL_SCHEMAS)
    assert schema["type"] == "function"
    assert schema["name"] == "propose_action_plan"
    assert schema["strict"] is True
    parameters = schema["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["actions"]
    assert parameters["additionalProperties"] is False
    actions = parameters["properties"]["actions"]
    assert actions["type"] == "array"
    assert actions["minItems"] == 2
    assert actions["maxItems"] == 3
    variants = actions["items"]["anyOf"]
    assert len(variants) == len(TEST_TOOL_SCHEMAS)
    tool_names = [v["properties"]["tool_name"]["enum"][0] for v in variants]
    assert tool_names == [tool["name"] for tool in TEST_TOOL_SCHEMAS]


@pytest.mark.parametrize(
    "response",
    (
        SimpleNamespace(output=[]),
        SimpleNamespace(output=None),
        SimpleNamespace(output=[function_call("open_app", "not-json")]),
        SimpleNamespace(output=[function_call("open_app", "[]")]),
        SimpleNamespace(output=[function_call("respond_conversationally", '{}')]),
    ),
)
def test_malformed_provider_response_fails_safely(response: object) -> None:
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(response)).resolve("request")


@pytest.mark.parametrize(
    "exception_name",
    (
        "APITimeoutError",
        "AuthenticationError",
        "RateLimitError",
        "APIConnectionError",
        "APIStatusError",
        "APIError",
    ),
)
def test_sdk_failures_are_normalized(monkeypatch, exception_name: str) -> None:
    class FakeSdkError(Exception):
        pass

    monkeypatch.setattr(provider_module, exception_name, FakeSdkError)
    provider = make_provider(FakeClient(error=FakeSdkError("private response detail")))

    with pytest.raises(IntentProviderUnavailableError):
        provider.resolve("request")


def test_provider_resolves_volume_and_media_controls() -> None:
    volume_response = SimpleNamespace(
        output=[function_call("volume_control", '{"action":"volume_up"}')]
    )
    result = make_provider(FakeClient(volume_response)).resolve("Turn up volume")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "volume_control"
    assert result.action.arguments == {"action": "volume_up"}

    plan_response = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"media_control","arguments":{"action":"play_pause"}}'
                ']}',
            )
        ]
    )
    result = make_provider(FakeClient(plan_response)).resolve("Open Spotify and play music")
    assert result.kind is IntentKind.ACTION_PLAN
    assert result.plan is not None
    assert len(result.plan.actions) == 2
    assert result.plan.actions[0].tool_name == "open_app"
    assert result.plan.actions[0].arguments == {"app_name": "Spotify"}
    assert result.plan.actions[1].tool_name == "media_control"
    assert result.plan.actions[1].arguments == {"action": "play_pause"}


def test_provider_resolves_system_status() -> None:
    status_response = SimpleNamespace(
        output=[function_call("system_status", '{"metric":"memory"}')]
    )
    result = make_provider(FakeClient(status_response)).resolve("How much RAM am I using?")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "system_status"
    assert result.action.arguments == {"metric": "memory"}

    plan_response = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"system_status","arguments":{"metric":"cpu"}},'
                '{"tool_name":"system_status","arguments":{"metric":"memory"}}'
                ']}',
            )
        ]
    )
    result = make_provider(FakeClient(plan_response)).resolve("Πες μου CPU και RAM")
    assert result.kind is IntentKind.ACTION_PLAN
    assert result.plan is not None
    assert len(result.plan.actions) == 2
    assert result.plan.actions[0].tool_name == "system_status"
    assert result.plan.actions[0].arguments == {"metric": "cpu"}
    assert result.plan.actions[1].tool_name == "system_status"
    assert result.plan.actions[1].arguments == {"metric": "memory"}


def test_provider_resolves_project_actions_and_plan() -> None:
    open_response = SimpleNamespace(
        output=[function_call("open_project", '{"project_name":"AiProject"}')]
    )
    result = make_provider(FakeClient(open_response)).resolve("Open AiProject in VS Code")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "open_project"
    assert result.action.arguments == {"project_name": "AiProject"}

    task_response = SimpleNamespace(
        output=[function_call("run_project_task", '{"project_name":"AiProject","task":"tests"}')]
    )
    result = make_provider(FakeClient(task_response)).resolve("Run tests for AiProject")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "run_project_task"
    assert result.action.arguments == {"project_name": "AiProject", "task": "tests"}

    plan_response = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_project","arguments":{"project_name":"AiProject"}},'
                '{"tool_name":"run_project_task","arguments":{"project_name":"AiProject","task":"tests"}}'
                ']}',
            )
        ]
    )
    result = make_provider(FakeClient(plan_response)).resolve(
        "Άνοιξε το AiProject στο VS Code και τρέξε τα tests."
    )
    assert result.kind is IntentKind.ACTION_PLAN
    assert result.plan is not None
    assert result.plan.actions[0].tool_name == "open_project"
    assert result.plan.actions[0].arguments == {"project_name": "AiProject"}
    assert result.plan.actions[1].tool_name == "run_project_task"
    assert result.plan.actions[1].arguments == {"project_name": "AiProject", "task": "tests"}


def test_provider_resolves_window_actions_and_plan() -> None:
    list_response = SimpleNamespace(
        output=[function_call("window_info", '{"action":"list"}')]
    )
    result = make_provider(FakeClient(list_response)).resolve("Τι παράθυρα είναι ανοιχτά;")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "window_info"
    assert result.action.arguments == {"action": "list"}

    active_response = SimpleNamespace(
        output=[function_call("window_info", '{"action":"active"}')]
    )
    result = make_provider(FakeClient(active_response)).resolve("Σε ποιο παράθυρο είμαι;")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "window_info"
    assert result.action.arguments == {"action": "active"}

    focus_response = SimpleNamespace(
        output=[function_call("focus_window", '{"query":"VS Code"}')]
    )
    result = make_provider(FakeClient(focus_response)).resolve("Πήγαινε στο VS Code.")
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "focus_window"
    assert result.action.arguments == {"query": "VS Code"}

    plan_response = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"focus_window","arguments":{"query":"VS Code"}}'
                ']}',
            )
        ]
    )
    result = make_provider(FakeClient(plan_response)).resolve(
        "Άνοιξε το Spotify και μετά γύρνα στο VS Code."
    )
    assert result.kind is IntentKind.ACTION_PLAN
    assert result.plan is not None
    assert len(result.plan.actions) == 2
    assert result.plan.actions[0].tool_name == "open_app"
    assert result.plan.actions[0].arguments == {"app_name": "Spotify"}
    assert result.plan.actions[1].tool_name == "focus_window"
    assert result.plan.actions[1].arguments == {"query": "VS Code"}



