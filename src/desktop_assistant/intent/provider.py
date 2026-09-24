from __future__ import annotations

from typing import Protocol

from desktop_assistant.intent.models import IntentResult


class IntentProviderError(RuntimeError):
    """Base error for safe intent-provider failures."""


class IntentProviderUnavailableError(IntentProviderError):
    """The provider could not serve the request."""


class MalformedIntentResponseError(IntentProviderError):
    """The provider returned output that cannot be trusted or interpreted."""


class IntentProvider(Protocol):
    def resolve(self, request: str) -> IntentResult: ...

    def decide_from_observation(self, request: str, observation: str) -> IntentResult: ...
