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
    {
        "type": "function",
        "name": "visual_inspect",
        "description": "Visually inspect one explicit existing window by taking a read-only screenshot and describing its visible contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Explicit window title or application name to inspect."},
                "goal": {"type": "string", "description": "Short visual question or description goal."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "visual_target",
        "description": "Locate one specific visible target inside one explicit existing window and return its normalized bounding box.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Explicit existing window title or application name."},
                "target": {"type": "string", "description": "Short description of the visible element to locate."},
            },
            "required": ["query", "target"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "visual_click",
        "description": "Locate and click one specific visible target inside an explicit window.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Explicit window title."},
                "target": {"type": "string", "description": "Visible element description."},
            },
            "required": ["query", "target"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "window_input",
        "description": "Send keyboard input to an existing window.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Target window."},
                "action": {"type": "string", "description": "Action."},
                "value": {"type": "string", "description": "Text value."},
            },
            "required": ["query", "action"],
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
    expected_tools = [tool["name"] for tool in TEST_TOOL_SCHEMAS if tool["name"] not in ("visual_inspect", "visual_target", "visual_click", "adaptive_ui_task")]
    assert len(variants) == len(expected_tools)
    tool_names = [v["properties"]["tool_name"]["enum"][0] for v in variants]
    assert tool_names == expected_tools
    assert "visual_inspect" not in tool_names
    assert "visual_target" not in tool_names
    assert "visual_click" not in tool_names
    assert "adaptive_ui_task" not in tool_names


def test_malformed_plan_containing_visual_inspect_rejected() -> None:
    plan_with_vision = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"visual_inspect","arguments":{"query":"Spotify"}}'
                ']}',
            )
        ]
    )
    with pytest.raises(MalformedIntentResponseError) as exc_info:
        make_provider(FakeClient(plan_with_vision)).resolve("Open Spotify and inspect visually")
    assert "visual_inspect" in str(exc_info.value)


def test_malformed_plan_containing_visual_target_rejected() -> None:
    plan_with_target = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":['
                '{"tool_name":"open_app","arguments":{"app_name":"Spotify"}},'
                '{"tool_name":"visual_target","arguments":{"query":"Spotify","target":"Play"}}'
                ']}',
            )
        ]
    )
    with pytest.raises(MalformedIntentResponseError) as exc_info:
        make_provider(FakeClient(plan_with_target)).resolve("Open Spotify and find Play button")
    assert "visual_target" in str(exc_info.value)


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


@pytest.mark.parametrize(
    "request_text",
    [
        "Άνοιξε τις ρυθμίσεις του Notepad.",
        "Open Notepad settings.",
        "Anoikse tis rythmiseis tou Notepad.",
        "Βρες το κουμπί για settings στο Notepad και άνοιξέ το.",
    ],
)
def test_provider_resolves_observe_ui_then_decide(request_text: str) -> None:
    response = SimpleNamespace(
        output=[function_call("observe_ui_then_decide", '{"query":"Notepad"}')]
    )
    result = make_provider(FakeClient(response)).resolve(request_text)
    assert result.kind is IntentKind.OBSERVE_UI_THEN_DECIDE
    assert result.query == "Notepad"


def test_provider_rejects_malformed_observe_ui() -> None:
    for bad_args in ('{"query":""}', '{"query":"   "}', "{}", '{"window":"Notepad"}'):
        response = SimpleNamespace(
            output=[function_call("observe_ui_then_decide", bad_args)]
        )
        with pytest.raises(MalformedIntentResponseError):
            make_provider(FakeClient(response)).resolve("Open Notepad settings.")


def test_provider_decide_from_observation_single_action() -> None:
    response = SimpleNamespace(
        output=[
            function_call(
                "ui_action",
                '{"query":"Notepad","control":"Settings","action":"invoke"}',
            )
        ]
    )
    fake_client = FakeClient(response)
    provider = make_provider(fake_client)

    obs = "1. button — Settings [invoke]\n2. check_box — Spell check [toggle]"
    result = provider.decide_from_observation("Open Notepad settings.", obs)

    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "ui_action"
    assert result.action.arguments == {
        "query": "Notepad",
        "control": "Settings",
        "action": "invoke",
    }

    # Verify call parameters
    assert len(fake_client.responses.calls) == 1
    call_kwargs = fake_client.responses.calls[0]
    assert "Original user request: Open Notepad settings." in str(call_kwargs["input"])
    assert obs in str(call_kwargs["input"])
    tool_names = [t.get("name") or t.get("function", {}).get("name") for t in call_kwargs["tools"]]
    assert "propose_action_plan" not in tool_names
    assert "observe_ui_then_decide" not in tool_names
    assert "ui_inspect" not in tool_names


def test_provider_decide_from_observation_conversational_and_unsupported() -> None:
    conv_response = SimpleNamespace(
        output=[function_call("respond_conversationally", '{"message":"I see the settings."}')]
    )
    result_conv = make_provider(FakeClient(conv_response)).decide_from_observation("Help", "1. button — Help")
    assert result_conv.kind is IntentKind.CONVERSATION
    assert result_conv.message == "I see the settings."

    unsup_response = SimpleNamespace(
        output=[function_call("report_unsupported", '{"message":"No settings button found."}')]
    )
    result_unsup = make_provider(FakeClient(unsup_response)).decide_from_observation("Settings", "1. text — Info")
    assert result_unsup.kind is IntentKind.UNSUPPORTED
    assert result_unsup.message == "No settings button found."


def test_provider_decide_from_observation_rejects_plan_or_second_observe() -> None:
    plan_response = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":[{"tool_name":"open_app","arguments":{"app_name":"Notepad"}},{"tool_name":"focus_window","arguments":{"query":"Notepad"}}]}',
            )
        ]
    )
    result_plan = make_provider(FakeClient(plan_response)).decide_from_observation("Settings", "obs")
    assert result_plan.kind is IntentKind.UNSUPPORTED
    assert "Observation cannot be chained or planned." in str(result_plan.message)

    observe_response = SimpleNamespace(
        output=[function_call("observe_ui_then_decide", '{"query":"Notepad"}')]
    )
    result_obs = make_provider(FakeClient(observe_response)).decide_from_observation("Settings", "obs")
    assert result_obs.kind is IntentKind.UNSUPPORTED
    assert "Observation cannot be chained or planned." in str(result_obs.message)

    inspect_response = SimpleNamespace(
        output=[function_call("ui_inspect", '{"query":"Notepad"}')]
    )
    result_inspect = make_provider(FakeClient(inspect_response)).decide_from_observation("Settings", "obs")
    assert result_inspect.kind is IntentKind.UNSUPPORTED
    assert "Observation cannot be chained or planned." in str(result_inspect.message)

    vis_response = SimpleNamespace(
        output=[function_call("visual_inspect", '{"query":"Notepad","goal":"Check"}')]
    )
    result_vis = make_provider(FakeClient(vis_response)).decide_from_observation("Settings", "obs")
    assert result_vis.kind is IntentKind.UNSUPPORTED
    assert "Observation cannot be chained or planned." in str(result_vis.message)

    target_response = SimpleNamespace(
        output=[function_call("visual_target", '{"query":"Notepad","target":"Save"}')]
    )
    result_target = make_provider(FakeClient(target_response)).decide_from_observation("Settings", "obs")
    assert result_target.kind is IntentKind.UNSUPPORTED
    assert "Observation cannot be chained or planned." in str(result_target.message)


def test_provider_observation_tools_exclude_visual_target() -> None:
    provider = make_provider(FakeClient(SimpleNamespace(output=[])))
    obs_tool_names = [t.get("name") for t in provider._observation_tools]
    assert "visual_target" not in obs_tool_names
    assert "visual_inspect" not in obs_tool_names
    assert "ui_inspect" not in obs_tool_names


def test_provider_decide_from_observation_rejects_multiple_calls() -> None:
    multi_response = SimpleNamespace(
        output=[
            function_call("ui_action", '{"query":"Notepad","control":"A","action":"invoke"}'),
            function_call("ui_action", '{"query":"Notepad","control":"B","action":"invoke"}'),
        ]
    )
    result = make_provider(FakeClient(multi_response)).decide_from_observation("Settings", "obs")
    assert result.kind is IntentKind.UNSUPPORTED
    assert "Multiple independent function calls are not supported." in str(result.message)


@pytest.mark.parametrize(
    ("request_text", "expected_query"),
    [
        ("Τι βλέπεις στο VS Code;", "VS Code"),
        ("Κοίτα το VS Code και πες μου τι έχει ανοιχτό.", "VS Code"),
        ("Koita to VS Code kai pes mou ti vlepeis.", "VS Code"),
        ("Look at VS Code and tell me what is visible.", "VS Code"),
    ],
)
def test_provider_resolves_visual_inspect(request_text: str, expected_query: str) -> None:
    response = SimpleNamespace(
        output=[
            function_call(
                "visual_inspect",
                f'{{"query":"{expected_query}","goal":"Describe what is visible."}}',
            )
        ]
    )
    result = make_provider(FakeClient(response)).resolve(request_text)
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "visual_inspect"
    assert result.action.arguments["query"] == expected_query


@pytest.mark.parametrize(
    ("request_text", "expected_query", "expected_target"),
    [
        ("Βρες το Search στο VS Code.", "VS Code", "Search"),
        ("Πού είναι το Search στο VS Code;", "VS Code", "Search"),
        ("Koita to VS Code kai vre mou to Search.", "VS Code", "Search"),
        ("Locate the Search icon in VS Code.", "VS Code", "Search icon"),
        ("Pou einai to Search sto VS Code?", "VS Code", "Search"),
    ],
)
def test_provider_resolves_visual_target(request_text: str, expected_query: str, expected_target: str) -> None:
    response = SimpleNamespace(
        output=[
            function_call(
                "visual_target",
                f'{{"query":"{expected_query}","target":"{expected_target}"}}',
            )
        ]
    )
    result = make_provider(FakeClient(response)).resolve(request_text)
    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "visual_target"
    assert result.action.arguments["query"] == expected_query
    assert result.action.arguments["target"] == expected_target


def test_provider_resolves_adaptive_ui_task() -> None:
    response = SimpleNamespace(
        output=[
            function_call(
                "adaptive_ui_task",
                '{"query":"VS Code","goal":"Click Search and type Bookish"}',
            )
        ]
    )
    result = make_provider(FakeClient(response)).resolve("Πάτα Search στο VS Code και μετά γράψε Bookish.")
    assert result.kind is IntentKind.ADAPTIVE_UI_TASK
    assert result.adaptive_task is not None
    assert result.adaptive_task.query == "VS Code"
    assert result.adaptive_task.goal == "Click Search and type Bookish"
    assert result.query == "VS Code"


def test_provider_rejects_malformed_adaptive_ui_task() -> None:
    bad_payloads = [
        '{"query":"","goal":"goal"}',
        '{"query":"VS Code","goal":""}',
        '{"query":"   ","goal":"goal"}',
        '{"query":"VS Code","goal":"   "}',
        '{"goal":"goal"}',
        '{"query":"VS Code"}',
        "{}",
    ]
    for bad_args in bad_payloads:
        response = SimpleNamespace(output=[function_call("adaptive_ui_task", bad_args)])
        with pytest.raises(MalformedIntentResponseError):
            make_provider(FakeClient(response)).resolve("some request")


def test_provider_decide_adaptive_ui_step_actions() -> None:
    for tool_name, args_json, expected_args in [
        (
            "ui_action",
            '{"query":"VS Code","control":"Search","action":"invoke"}',
            {"query": "VS Code", "control": "Search", "action": "invoke"},
        ),
        (
            "visual_click",
            '{"query":"VS Code","target":"Search"}',
            {"query": "VS Code", "target": "Search"},
        ),
        (
            "window_input",
            '{"query":"VS Code","action":"type_text","value":"Bookish"}',
            {"query": "VS Code", "action": "type_text", "value": "Bookish"},
        ),
    ]:
        response = SimpleNamespace(output=[function_call(tool_name, args_json)])
        client = FakeClient(response)
        provider = make_provider(client)
        result = provider.decide_adaptive_ui_step("req", "VS Code", 1, "obs", "hist")
        assert result.kind is IntentKind.TOOL_ACTION
        assert result.action is not None
        assert result.action.tool_name == tool_name
        assert result.action.arguments == expected_args


def test_provider_decide_adaptive_ui_step_complete_and_unsupported() -> None:
    comp_response = SimpleNamespace(output=[function_call("report_complete", '{"message":"Task finished."}')])
    result_comp = make_provider(FakeClient(comp_response)).decide_adaptive_ui_step("req", "VS Code", 2, "obs", "hist")
    assert result_comp.kind is IntentKind.COMPLETE
    assert result_comp.message == "Task finished."

    unsup_response = SimpleNamespace(output=[function_call("report_unsupported", '{"message":"Cannot find search."}')])
    result_unsup = make_provider(FakeClient(unsup_response)).decide_adaptive_ui_step("req", "VS Code", 1, "obs", "hist")
    assert result_unsup.kind is IntentKind.UNSUPPORTED
    assert result_unsup.message == "Cannot find search."


def test_provider_decide_adaptive_ui_step_rejects_plan_and_recursion() -> None:
    for forbidden in (
        "propose_action_plan",
        "observe_ui_then_decide",
        "adaptive_ui_task",
        "ui_inspect",
        "visual_inspect",
        "visual_target",
    ):
        response = SimpleNamespace(output=[function_call(forbidden, '{"query":"VS Code"}')])
        result = make_provider(FakeClient(response)).decide_adaptive_ui_step("req", "VS Code", 1, "obs", "hist")
        assert result.kind is IntentKind.UNSUPPORTED
        assert "Actions cannot be chained or planned inside an adaptive UI task." in str(result.message)


def test_provider_decide_adaptive_ui_step_rejects_disallowed_tools() -> None:
    response = SimpleNamespace(output=[function_call("open_app", '{"app_name":"Notepad"}')])
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(response)).decide_adaptive_ui_step("req", "VS Code", 1, "obs", "hist")


def test_provider_plan_schema_and_parser_rejects_adaptive_task() -> None:
    plan_response = SimpleNamespace(
        output=[
            function_call(
                "propose_action_plan",
                '{"actions":[{"tool_name":"open_app","arguments":{"app_name":"Notepad"}},{"tool_name":"adaptive_ui_task","arguments":{"query":"VS Code","goal":"test"}}]}',
            )
        ]
    )
    with pytest.raises(MalformedIntentResponseError):
        make_provider(FakeClient(plan_response)).resolve("req")







