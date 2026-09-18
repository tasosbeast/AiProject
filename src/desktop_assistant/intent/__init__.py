"""Optional natural-language intent providers."""

from desktop_assistant.intent.models import IntentKind, IntentResult, ToolAction
from desktop_assistant.intent.provider import IntentProvider, IntentProviderError

__all__ = [
    "IntentKind",
    "IntentProvider",
    "IntentProviderError",
    "IntentResult",
    "ToolAction",
]
