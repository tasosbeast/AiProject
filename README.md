# Windows Personal AI Assistant

A small, security-minded foundation for a future personal AI desktop assistant.
It includes a native Windows desktop chat interface and a command-line interface.
Both use the same deterministic assistant core and the same deliberately narrow
set of explicit local tools.

Despite the long-term name, this version does **not** call an AI model. The
command router is intentionally simple so the safety and tool boundaries are
clear before natural-language model routing is introduced.

## What it can do

- Open an allowlisted Windows application: Chrome, Spotify, VS Code, File
  Explorer, or Notepad.
- Open an existing folder after validating the path.
- Open an `http` or `https` website after validating the URL.
- Accept commands through a native, responsive PySide6 desktop interface.
- Show help and exit cleanly.

It never turns user input into a PowerShell, Command Prompt, or shell command.

## What it does not do yet

- Voice input or speech output
- Browser or mouse/keyboard automation
- Persistent memory
- An LLM or natural-language AI routing
- Arbitrary executable or shell-command execution
- File creation, modification, deletion, or overwrite

## Requirements

- Windows 10 or Windows 11
- Python 3.11 or newer

## Setup

From PowerShell in this repository:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,gui]"
```

The application currently needs no secrets. `.env.example` documents the only
optional setting for this milestone. Environment files containing local values
are ignored by Git.

## Run the desktop GUI

After installation:

```powershell
windows-assistant-gui
```

Or run it directly from the source tree:

```powershell
$env:PYTHONPATH = "src"
python -m desktop_assistant.gui
```

The GUI uses a background Qt worker for command processing, so the window stays
responsive while the shared assistant core runs. Conversation history exists
only for the current session.

> Screenshot placeholder: add a current application screenshot after the visual
> design is finalized for the first packaged release.

## Run the CLI

After installation:

```powershell
windows-assistant
```

Or run directly from the source tree:

```powershell
$env:PYTHONPATH = "src"
python -m desktop_assistant
```

Example commands:

```text
open spotify
open app notepad
open folder C:\Users\YourName\Documents
open website https://www.python.org
help
exit
```

## Tests

```powershell
python -m pytest
```

The test suite uses fake assistants and a fake system launcher. It does not
actually open programs, folders, or browser windows. GUI tests run using Qt's
offscreen platform and avoid pixel-perfect assertions.

## Architecture

```text
src/desktop_assistant/
  assistant.py    Application-facing orchestrator
  bootstrap.py    Shared production composition for CLI and GUI
  router.py       Deterministic command parsing and dispatch
  tools.py        Explicit open-app, open-folder, and open-website tools
  launcher.py     Windows-only operating-system boundary
  config.py       Central application allowlist and environment settings
  models.py       Risk levels and structured tool results
  safety.py       Central policy that gates tools by risk level
  cli.py          Interactive command-line loop
  gui/            Native Qt window, widgets, worker, and stylesheet
```

The desktop UI and CLI both call the same `Assistant.handle()` method. The GUI
does not parse or execute commands itself. Tools contain no AI logic: each tool
declares a risk level, validates its own input, and calls a narrow launcher
interface. Today all three tools are `SAFE`. The risk model leaves room for
confirmation policies when sensitive actions are added later.

## Roadmap

1. Add an intent-provider interface and optional LLM routing with structured
   tool calls; keep deterministic routing as a fallback.
2. Add a confirmation workflow before introducing any sensitive actions.
3. Add session context and explicitly managed preferences.
4. Add voice input/output.
5. Package the stable application as a Windows executable.

Any future destructive capability should require an explicit confirmation and
an audit-friendly record of the requested action.
