from __future__ import annotations

from types import SimpleNamespace

import pytest

import desktop_assistant.intent.openai_provider as provider_module
from desktop_assistant.intent.models import IntentKind
from desktop_assistant.intent.openai_provider import OpenAIIntentProvider
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


def make_provider(client: FakeClient) -> OpenAIIntentProvider:
    return OpenAIIntentProvider(
        api_key="test-key",
        model="test-model",
        tool_schemas=[],
        client=client,
    )


def test_provider_uses_stateless_responses_api_with_single_call_settings() -> None:
    client = FakeClient(SimpleNamespace(output=[function_call("open_app", '{"app_name":"Spotify"}')]))

    result = make_provider(client).resolve("Please open Spotify")

    assert result.kind is IntentKind.TOOL_ACTION
    assert result.action is not None
    assert result.action.tool_name == "open_app"
    request = client.responses.calls[0]
    assert request["model"] == "test-model"
    assert request["store"] is False
    assert request["parallel_tool_calls"] is True
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


def test_two_and_three_tool_calls_yield_action_plan_in_order() -> None:
    two_calls = SimpleNamespace(
        output=[
            function_call("open_app", '{"app_name":"Spotify"}'),
            function_call("open_app", '{"app_name":"Chrome"}'),
        ]
    )
    result_two = make_provider(FakeClient(two_calls)).resolve("Open Spotify and Chrome")
    assert result_two.kind is IntentKind.ACTION_PLAN
    assert result_two.plan is not None
    assert len(result_two.plan.actions) == 2
    assert result_two.plan.actions[0].tool_name == "open_app"
    assert result_two.plan.actions[0].arguments == {"app_name": "Spotify"}
    assert result_two.plan.actions[1].tool_name == "open_app"
    assert result_two.plan.actions[1].arguments == {"app_name": "Chrome"}

    three_calls = SimpleNamespace(
        output=[
            function_call("open_app", '{"app_name":"Chrome"}'),
            function_call("open_folder", '{"path":"Downloads"}'),
            function_call("list_folder", '{"path":"Downloads"}'),
        ]
    )
    result_three = make_provider(FakeClient(three_calls)).resolve(
        "Άνοιξε το Chrome, άνοιξε τα Downloads και δείξε μου τα αρχεία"
    )
    assert result_three.kind is IntentKind.ACTION_PLAN
    assert result_three.plan is not None
    assert len(result_three.plan.actions) == 3
    assert [a.tool_name for a in result_three.plan.actions] == [
        "open_app",
        "open_folder",
        "list_folder",
    ]


def test_more_than_three_tool_calls_are_unsupported() -> None:
    four_calls = SimpleNamespace(
        output=[
            function_call("open_app", '{"app_name":"Chrome"}'),
            function_call("open_app", '{"app_name":"Spotify"}'),
            function_call("open_app", '{"app_name":"VS Code"}'),
            function_call("open_app", '{"app_name":"Notepad"}'),
        ]
    )
    result = make_provider(FakeClient(four_calls)).resolve("Open four apps")
    assert result.kind is IntentKind.UNSUPPORTED
    assert "at most 3 actions" in (result.message or "")


def test_mixed_control_and_tool_calls_are_unsupported() -> None:
    mixed = SimpleNamespace(
        output=[
            function_call("open_app", '{"app_name":"Chrome"}'),
            function_call("respond_conversationally", '{"message":"hello"}'),
        ]
    )
    result = make_provider(FakeClient(mixed)).resolve("Mixed request")
    assert result.kind is IntentKind.UNSUPPORTED


def test_multiple_control_calls_are_unsupported() -> None:
    multi_control = SimpleNamespace(
        output=[
            function_call("respond_conversationally", '{"message":"hello"}'),
            function_call("respond_conversationally", '{"message":"world"}'),
        ]
    )
    result = make_provider(FakeClient(multi_control)).resolve("Multi control")
    assert result.kind is IntentKind.UNSUPPORTED


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
