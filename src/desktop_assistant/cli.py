from __future__ import annotations

import logging

from desktop_assistant.assistant import Assistant
from desktop_assistant.config import AppCatalog, Settings
from desktop_assistant.launcher import WindowsSystemLauncher
from desktop_assistant.router import CommandRouter
from desktop_assistant.tools import OpenAppTool, OpenFolderTool, OpenWebsiteTool


def build_assistant() -> Assistant:
    launcher = WindowsSystemLauncher()
    catalog = AppCatalog()
    router = CommandRouter(
        OpenAppTool(launcher, catalog),
        OpenFolderTool(launcher),
        OpenWebsiteTool(launcher),
    )
    return Assistant(router)


def main() -> int:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    assistant = build_assistant()

    print("Assistant > What would you like me to do? Type 'help' for commands.")
    while True:
        try:
            command = input("User > ")
        except (EOFError, KeyboardInterrupt):
            print("\nAssistant > Goodbye.")
            return 0

        if command.strip().casefold() in {"exit", "quit"}:
            print("Assistant > Goodbye.")
            return 0

        result = assistant.handle(command)
        print(f"Assistant > {result.message}")


if __name__ == "__main__":
    raise SystemExit(main())

