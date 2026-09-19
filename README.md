# Windows Personal AI Assistant

A small, security-minded foundation for a future personal AI desktop assistant.
It includes a native Windows desktop chat interface and a command-line interface.
Both use the same assistant core, a deliberately narrow set of explicit local
tools, and optional OpenAI-powered natural-language intent routing.

The desktop GUI also supports explicit push-to-talk voice commands and optional
spoken responses. Voice remains an input/output adapter: every recognized
transcript is visibly displayed and passed unchanged to the same
`Assistant.handle()` path used by typed commands.

Exact commands are handled locally first. When an OpenAI API key is configured,
unrecognized natural-language requests can be classified into one registered
tool action, a short assistant-related response, or an unsupported result. The
model proposes intent only; it never executes Windows actions.

## What it can do

- Open an allowlisted Windows application: Chrome, Spotify, VS Code, File
  Explorer, or Notepad.
- Open an existing folder after validating the path.
- Open an `http` or `https` website after validating the URL.
- List up to 100 direct entries in an existing local folder without recursion.
- Check whether a local path exists and whether it is a file or folder.
- Create exactly one folder after explicit confirmation.
- Rename a file or folder within its current directory after confirmation.
- Move a file or folder to another directory on the same volume after confirmation.
- Accept commands through a native, responsive PySide6 desktop interface.
- Record a bounded push-to-talk voice command from the Windows microphone.
- Transcribe primarily Greek speech with natural English technical code-switching.
- Optionally speak the assistant result without blocking the GUI.
- Require explicit confirmation for every sensitive or destructive action.
- Understand simple English, Greek, Greeklish, and mixed requests when OpenAI is configured.
- Answer narrow questions about its current identity and capabilities.
- Show help and exit cleanly.

It never turns user input into a PowerShell, Command Prompt, or shell command.

## What it does not do yet

- Realtime, always-listening, or wake-word voice interaction
- Browser or mouse/keyboard automation
- Persistent memory
- Broad knowledge questions, web search, or general-purpose chat
- Multiple computer actions from a single request
- Arbitrary executable or shell-command execution
- File content reading or writing
- File or folder deletion, recursive removal, or recycle-bin operations
- Copying files or cross-volume moves
- Overwriting or merging existing destinations

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
OPENAI_TRANSCRIBE_MODEL=gpt-transcribe
OPENAI_TTS_MODEL=gpt-4o-mini-tts
OPENAI_TTS_VOICE=marin
VOICE_OUTPUT_ENABLED=true
ASSISTANT_LOG_LEVEL=INFO
```

Never commit `.env.local`; it is ignored by Git. Existing operating-system or
process environment variables take precedence over `.env.local`. The default
model is `gpt-5.6-luna`, and it can be replaced through `OPENAI_MODEL`.

The app does not contact OpenAI during startup. Without a key, exact deterministic
commands continue to work offline; natural-language fallback remains unavailable.
OpenAI API usage and billing are separate from a ChatGPT subscription.

`gpt-transcribe` is the default speech-to-text model. It receives expected
Greek (`el`) and English (`en`) language hints plus a short, application-owned
keyword list for names such as Spotify, Chrome, VS Code, GitHub, and Downloads.
The context asks for faithful modern Greek transcription with natural English
technical terms; it does not request translation or run a second correction
model over the transcript.

`gpt-4o-mini-tts` with the `marin` voice is the default speech-output setup.
Set `VOICE_OUTPUT_ENABLED=false` to skip TTS, or change the model and voice with
the variables above. Audio requests use a 20-second timeout and one retry.

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

### Push-to-talk workflow

1. Click **Mic** to begin recording. The status changes to `Listening...`.
2. Click **Stop** to finish. The status moves through `Transcribing...` and
   `Working...`.
3. The recognized transcript appears as the user's message and is sent exactly
   once through `Assistant.handle()`.
4. If voice output is enabled, the assistant result is generated and played
   while the status says `Speaking...`, then the app returns to `Ready`.

Recording never starts automatically. It is limited to 60 seconds and is not
streamed while idle. Qt Multimedia records a temporary mono WAV using the native
Windows microphone path. Recordings are deleted after success, failure, cancel,
or shutdown where possible. Generated speech is also stored only in a temporary
playback file and removed after playback. No voice history is retained.

If no microphone, API key, or speech device is available, typed GUI commands and
the CLI continue to work. Voice errors appear in the conversation rather than in
modal dialogs.

### Confirmation and permission model

Every registered tool declares an application-owned risk level:

- `SAFE`: executes after normal registry validation, without a prompt.
- `SENSITIVE`: is prepared and requires explicit confirmation before execution.
- `DESTRUCTIVE`: follows the same guarded flow with stronger warning text and
  visual treatment.

Confirmation authorizes exactly one immutable, already-prepared action. The
assistant does not call the intent provider again, re-transcribe audio, or
reconstruct arguments after approval. The model cannot choose a risk level or
write confirmation text; both come from trusted tool metadata. Invalid actions
fail before a confirmation is shown, and tools may perform final safety checks
immediately before execution to reduce time-of-check/time-of-use risk.

Only one confirmation may be pending. It is held in memory for up to two minutes
using a monotonic timer and is removed after confirmation, cancellation, expiry,
or application shutdown. Tokens are opaque and single-use; nothing is persisted
to disk. While a confirmation card is visible, GUI text input and microphone
capture are disabled. Voice can request an action but cannot authorize it: a
physical **Confirm** click is required. The CLI displays the exact action and
risk, then uses a local `Confirm? [y/N]:` prompt; its response is never sent to
the AI provider.

The production filesystem capability pack is intentionally narrow:

| Tool | Risk | Confirmation | Behavior |
| --- | --- | --- | --- |
| `list_folder` | SAFE | No | Lists at most 100 direct entries; never recurses or reads contents. |
| `path_exists` | SAFE | No | Reports exists/file/folder only. |
| `create_folder` | SENSITIVE | Yes | Creates one folder; its parent must already exist. |
| `rename_path` | SENSITIVE | Yes | Renames within the same parent folder. |
| `move_path` | SENSITIVE | Yes | Moves to another folder on the same volume. |

All mutation tools reject destination collisions, directory merges, device/UNC
paths, symlinks or junctions in the mutation path, and protected Windows,
Program Files, ProgramData, or drive-root locations. They never overwrite.
Deletion remains completely unsupported, and no production filesystem tool is
classified `DESTRUCTIVE` in this milestone.

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
list folder Downloads
check path C:\Users\YourName\Downloads\manual.pdf
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
Create a Projects folder on my Desktop.
Rename C:\Users\YourName\Downloads\draft.txt to C:\Users\YourName\Downloads\final.txt.
Move C:\Users\YourName\Downloads\manual.pdf to C:\Users\YourName\Documents\manual.pdf.
Φτιάξε έναν φάκελο Projects στο Desktop.
Τι έχει μέσα ο φάκελος Downloads;
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
  confirmation.py In-memory exact-action confirmation manager
  filesystem.py  Shared path validation and protected-location policy
  filesystem_tools.py Safe queries and confirmed filesystem mutations
  bootstrap.py    Shared production composition for CLI and GUI
  router.py       Deterministic command parsing and dispatch
  tool_registry.py Authoritative schemas, validation, safety, and execution
  tools.py        Explicit open-app, open-folder, and open-website tools
  launcher.py     Windows-only operating-system boundary
  config.py       Central application allowlist and environment settings
  models.py       Risk levels, interaction responses, and structured tool results
  safety.py       ALLOW / REQUIRE_CONFIRMATION / DENY policy decisions
  cli.py          Interactive command-line loop
  gui/            Native Qt window, workers, audio adapters, and stylesheet
  intent/         Provider-neutral intent models and isolated OpenAI adapter
  voice/          Audio models, provider contracts, and isolated OpenAI audio adapter
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

`ToolDefinition` owns an ordered tuple of typed named argument definitions.
That same metadata generates strict OpenAI schemas, required fields, type checks,
known-folder resolution, and unexpected-field rejection. Tool preparation then
returns immutable `ToolArguments` plus a deeply immutable payload. Sensitive
filesystem payloads are frozen dataclasses containing only `Path`, immutable
identity records, strings, and tuples; mutable dictionaries and lists are rejected
by the registry before a confirmation can be created.

Before confirmed execution, mutation tools revalidate protected locations,
source identity, destination absence, destination-parent identity, operation
semantics, and volume. Rename and move use `os.rename` on the Windows target and
never use `shutil.move`, so cross-volume copy-plus-delete behavior is refused.
These checks reduce, but cannot completely eliminate, filesystem TOCTOU races or
all Windows reparse-point behavior; ambiguous or unsupported cases fail closed.

Voice capture and playback use PySide6 Qt Multimedia, so no separate native
microphone framework is required. Transcription, assistant execution, TTS
generation, and playback are explicit sequential states. A transcription error
executes no assistant action; a TTS or playback error cannot undo a completed
tool action.

Confirmation requests are also core results: GUI and CLI only present them and
call `Assistant.confirm(id)` or `Assistant.cancel(id)` directly. They never turn
button clicks or CLI answers into new natural-language requests. Audit-friendly
logs record the tool name, risk level, shortened correlation ID, and outcome,
but omit action arguments and secrets.

## Roadmap

1. Review the filesystem capability pack and its protected-location policy.
2. Design deletion separately with recycle-bin semantics and stronger destructive confirmation.
3. Add explicitly managed, non-sensitive session preferences without persistent memory.
4. Improve full-application localization while keeping transcripts faithful.

Confirmation reduces authorization ambiguity but cannot eliminate every external
time-of-check/time-of-use change. Future filesystem mutation tools must repeat
relevant safety checks immediately before their side effect.
