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


def test_multiple_function_calls_are_blocked() -> None:
    response = SimpleNamespace(
        output=[
            function_call("open_app", '{"app_name":"Spotify"}'),
            function_call("open_app", '{"app_name":"Chrome"}'),
        ]
    )

    result = make_provider(FakeClient(response)).resolve("Open Spotify and Chrome")

    assert result.kind is IntentKind.UNSUPPORTED
    assert "only one" in (result.message or "")


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
