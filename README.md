# Windows Personal AI Assistant

A small, security-minded foundation for a future personal AI desktop assistant.
The current milestone is a deterministic command-line application: it recognizes
a deliberately narrow set of commands and invokes only explicit local tools.

Despite the long-term name, this version does **not** call an AI model. The
command router is intentionally simple so the safety and tool boundaries are
clear before natural-language model routing is introduced.

## What it can do

- Open an allowlisted Windows application: Chrome, Spotify, VS Code, File
  Explorer, or Notepad.
- Open an existing folder after validating the path.
- Open an `http` or `https` website after validating the URL.
- Show help and exit cleanly.

It never turns user input into a PowerShell, Command Prompt, or shell command.

## What it does not do yet

- Voice input or speech output
- A graphical interface
- Browser or mouse/keyboard automation
- Persistent memory
- LLM-based intent detection
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
python -m pip install -e ".[dev]"
```

The application currently needs no secrets. `.env.example` documents the only
optional setting for this milestone. Environment files containing local values
are ignored by Git.

## Run

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

The test suite uses a fake system launcher. It does not actually open programs,
folders, or browser windows.

## Architecture

```text
src/desktop_assistant/
  assistant.py    Application-facing orchestrator
  router.py       Deterministic command parsing and dispatch
  tools.py        Explicit open-app, open-folder, and open-website tools
  launcher.py     Windows-only operating-system boundary
  config.py       Central application allowlist and environment settings
  models.py       Risk levels and structured tool results
  safety.py       Central policy that gates tools by risk level
  cli.py          Interactive command-line loop
```

Tools contain no AI logic. Each tool declares a risk level, validates its own
input, and calls a narrow launcher interface. Today all three tools are `SAFE`.
The risk model leaves room for confirmation policies when sensitive actions are
added later.

## Roadmap

1. Add a confirmation service and a small set of sensitive, reversible actions.
2. Add an intent-provider interface and optional LLM routing with structured
   tool calls; keep deterministic routing as a fallback.
3. Add session context and explicitly managed preferences.
4. Add voice input/output.
5. Add a Windows GUI and carefully scoped browser/application automation.

Any future destructive capability should require an explicit confirmation and
an audit-friendly record of the requested action.
