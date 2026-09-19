from __future__ import annotations

import logging
from collections.abc import Callable

from desktop_assistant.assistant import Assistant
from desktop_assistant.bootstrap import build_assistant
from desktop_assistant.config import load_settings
from desktop_assistant.models import AssistantResponse, AssistantResponseKind


def present_response(
    assistant: Assistant,
    response: AssistantResponse,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Present one core response and resolve confirmation locally, never through routing."""

    if response.kind is AssistantResponseKind.COMPLETED:
        if response.result is not None:
            output_fn(f"Assistant > {response.result.message}")
        return

    request = response.confirmation
    if request is None:
        output_fn("Assistant > That confirmation could not be displayed safely.")
        return
    output_fn("Assistant > Confirmation required")
    output_fn(f"Action: {request.summary}")
    output_fn(f"Risk: {request.risk_level.value.title()}")
    if request.warning:
        output_fn(request.warning)
    try:
        answer = input_fn("Confirm? [y/N]: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    final = (
        assistant.confirm(request.confirmation_id)
        if answer in {"y", "yes"}
        else assistant.cancel(request.confirmation_id)
    )
    if final.result is not None:
        output_fn(f"Assistant > {final.result.message}")


def main() -> int:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    assistant = build_assistant(settings=settings)

    print("Assistant > What would you like me to do? Type 'help' for commands.")
    try:
        while True:
            try:
                command = input("User > ")
            except (EOFError, KeyboardInterrupt):
                print("\nAssistant > Goodbye.")
                return 0

            if command.strip().casefold() in {"exit", "quit"}:
                print("Assistant > Goodbye.")
                return 0

            present_response(assistant, assistant.handle(command))
    finally:
        assistant.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
