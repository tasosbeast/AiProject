# Windows Personal AI Assistant

A small, security-minded foundation for a future personal AI desktop assistant.
It includes a native Windows desktop chat interface and a command-line interface.
Both use the same assistant core, a deliberately narrow set of explicit local
tools, and optional OpenAI-powered natural-language intent routing.

Exact commands are handled locally first. When an OpenAI API key is configured,
unrecognized natural-language requests can be classified into one registered
tool action, a short assistant-related response, or an unsupported result. The
model proposes intent only; it never executes Windows actions.

## What it can do

- Open an allowlisted Windows application: Chrome, Spotify, VS Code, File
  Explorer, or Notepad.
- Open an existing folder after validating the path.
- Open an `http` or `https` website after validating the URL.
- Accept commands through a native, responsive PySide6 desktop interface.
- Understand simple English, Greek, Greeklish, and mixed requests when OpenAI is configured.
- Answer narrow questions about its current identity and capabilities.
- Show help and exit cleanly.

It never turns user input into a PowerShell, Command Prompt, or shell command.

## What it does not do yet

- Voice input or speech output
- Browser or mouse/keyboard automation
- Persistent memory
- Broad knowledge questions, web search, or general-purpose chat
- Multiple computer actions from a single request
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

## Optional OpenAI configuration

Copy `.env.example` to `.env.local` and add your project API key:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.6-luna
ASSISTANT_LOG_LEVEL=INFO
```

Never commit `.env.local`; it is ignored by Git. Existing operating-system or
process environment variables take precedence over `.env.local`. The default
model is `gpt-5.6-luna`, and it can be replaced through `OPENAI_MODEL`.

The app does not contact OpenAI during startup. Without a key, exact deterministic
commands continue to work offline; natural-language fallback remains unavailable.
OpenAI API usage and billing are separate from a ChatGPT subscription.

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

With OpenAI configured, examples also include:

```text
Could you open Spotify for me?
Vale mou to Spotify.
Anoikse mou ton Chrome.
Go to python.org.
What can you currently do?
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
  tool_registry.py Authoritative schemas, validation, safety, and execution
  tools.py        Explicit open-app, open-folder, and open-website tools
  launcher.py     Windows-only operating-system boundary
  config.py       Central application allowlist and environment settings
  models.py       Risk levels and structured tool results
  safety.py       Central policy that gates tools by risk level
  cli.py          Interactive command-line loop
  gui/            Native Qt window, widgets, worker, and stylesheet
  intent/         Provider-neutral intent models and isolated OpenAI adapter
```

The desktop UI and CLI both call the same `Assistant.handle()` method. The GUI
does not import OpenAI, parse commands, or execute actions itself.

The deterministic router always runs first. A recognized command—including a
recognized command rejected by validation—never falls through to OpenAI. Only an
unrecognized request may use the optional provider. Model tool calls are treated
as untrusted and must pass the registry's exact name, argument, type, unexpected-
field, one-action, and `SafetyPolicy` checks before an existing tool can run.

The Responses API request is stateless, uses strict function schemas, disables
parallel tool calls, has a 15-second timeout, and permits one retry. Timeout,
authentication, rate-limit, connection, unavailable-model, and malformed-output
failures produce a concise `AI routing is temporarily unavailable.` result while
the GUI returns to `Ready`.

## Roadmap

1. Add a confirmation workflow before introducing any sensitive actions.
2. Add explicitly managed, non-sensitive session preferences without persistent memory.
3. Add voice input and speech output behind the same assistant boundary.
4. Package the stable application as a Windows executable.

Any future destructive capability should require an explicit confirmation and
an audit-friendly record of the requested action.
