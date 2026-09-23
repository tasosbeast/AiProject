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
- Check whether an allowlisted application is currently running.
- Request a graceful close of allowlisted application windows after confirmation.
- Remain available in the Windows system tray when the main window is hidden.
- Show, restore, and focus the assistant with `Ctrl+Alt+Space`.
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
- General browser or mouse automation
- Persistent memory
- Broad knowledge questions, web search, or general-purpose chat
- Multiple computer actions from a single request
- Arbitrary executable or shell-command execution
- File content reading or writing
- File or folder deletion, recursive removal, or recycle-bin operations
- Copying files or cross-volume moves
- Overwriting or merging existing destinations
- Force-killing processes, accepting arbitrary PIDs, or closing the Explorer shell
- Starting automatically with Windows

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

For packaging development, install the build extra as well:

```powershell
python -m pip install -e ".[dev,gui,build]"
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
SYSTEM_TRAY_ENABLED=true
GLOBAL_HOTKEY=Ctrl+Alt+Space
ASSISTANT_LOG_LEVEL=INFO
```

Never commit `.env.local`; it is ignored by Git. Existing operating-system or
process environment variables take precedence over `.env.local`. In source
development, the application resolves this file from the repository root based
on the installed module location, not the shell's current working directory.
The default model is `gpt-5.6-luna`, and it can be replaced through
`OPENAI_MODEL`.

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

## Packaged Windows application

The supported packaged distribution is a PyInstaller 6.x **onedir** bundle.
It is intentionally not a one-file executable: Qt Multimedia, Windows audio
backends, OpenAI provider modules, and their runtime libraries remain in a
predictable folder beside a small windowed launcher. The packaged application
opens without a console window.

Build from any PowerShell working directory:

```powershell
& "C:\path\to\AiProject\scripts\build_windows.ps1" -Clean
```

The output is:

```text
dist\AiAssistant\AiAssistant.exe
```

Distribute or relocate the complete `dist\AiAssistant` directory, not only the
`.exe`. The build script verifies that the executable exists and fails if the
bundle contains `.env.local` or an obvious OpenAI API-key-shaped value. Neither
`build/` nor `dist/` is tracked by Git. The build is reproducible from the
checked-in `packaging\AiAssistant.spec`; it contains no developer-machine
absolute paths. PyInstaller `6.22.x` is the tested packaging line.

### Packaged configuration and logs

The packaged executable never loads `.env.local` from its current working
directory, its installation folder, or parent directories. It uses this
per-user writable location instead:

```text
%LOCALAPPDATA%\AiProject\AI Assistant\.env.local
%LOCALAPPDATA%\AiProject\AI Assistant\logs\assistant.log
```

Copy `.env.example` to that `.env.local` path and add the API key only on the
local machine. Operating-system/process environment variables still take
precedence, followed by this file, then application defaults. This makes the
configuration stable when the complete onedir bundle is moved and keeps secrets
and logs out of the installed files. Packaged logs rotate at about 1.5 MB with
three backups; they contain operational categories and failures but must not
contain API keys, authorization headers, audio, or file contents.

Without a key, the packaged GUI, deterministic commands, filesystem tools,
tray, hotkey, and local confirmations continue to work. OpenAI intent routing,
transcription, and TTS remain unavailable until configured. A startup failure is
logged where possible and shown as a concise Windows dialog rather than being
lost because the app has no console.

### System tray and global shortcut

When `SYSTEM_TRAY_ENABLED=true` and Windows reports that a system tray is
available, the normal window **X** hides the assistant instead of exiting. The
tray menu contains **Show Assistant**, **Hide Assistant**, and **Quit**. Only
explicit **Quit** stops microphone recording and speech playback, removes
temporary audio, discards a pending confirmation, unregisters the global
hotkey, and exits the process. A pending confirmation otherwise remains visible
after hide/show until it is answered or expires normally.

The default global shortcut is `Ctrl+Alt+Space`, configurable with
`GLOBAL_HOTKEY`. It only shows/restores the window, brings it forward, and
focuses the command input. It never starts the microphone, executes a command,
or contacts OpenAI. If Windows cannot register the shortcut because it is
invalid or already owned, the GUI, tray, typed input, and microphone continue
working without retrying in a loop. Hiding while actively listening cancels the
recording so the microphone never continues invisibly; already-started
transcription, assistant work, and TTS may finish normally.

If the system tray is unavailable or disabled, the normal window close exits
cleanly. Startup-with-Windows is intentionally not implemented.

Only one GUI instance is intended to run per Windows user session. A second
launch sends a narrow local IPC activation signal to the existing instance and
then exits before constructing the assistant or OpenAI/audio providers. The
first instance shows/restores the window and focuses the command input; it does
not execute a command or activate the microphone. Stale local-instance state is
recovered when its former owner is no longer running.

During explicit Quit, the window enters a shutdown state before cleanup. It
rejects new commands and microphone starts, clears queued background work,
cancels recording, stops playback, removes known temporary audio, discards the
pending confirmation, and unregisters the hotkey. Results that finish late are
ignored, so they cannot mutate the closed UI, start TTS, or create a new
confirmation. In-flight network calls are bounded by their existing provider
timeouts rather than being forcefully terminated.

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
paths, and any symbolic link, junction, mount point, or other Windows reparse
point in a mutation-relevant existing path chain. Reparse metadata is inspected
directly with the Python standard library, including on supported Python 3.11
runtimes. Every Windows drive root is protected independently of the system
drive, and Windows, Program Files, and ProgramData trees remain protected. The
normal user-profile folders remain usable. Mutation tools never overwrite.
Deletion remains completely unsupported, and no production filesystem tool is
classified `DESTRUCTIVE` in this milestone.

Application process control is also deliberately narrow:

| Tool | Risk | Confirmation | Behavior |
| --- | --- | --- | --- |
| `app_status` | SAFE | No | Reports only whether a catalog application is running. |
| `close_app` | SENSITIVE | Yes | Sends `WM_CLOSE` to all visible top-level windows owned by the catalog application's trusted process names. |

The application catalog owns immutable process-name metadata. Neither users nor
the model can provide PIDs, executable paths, process names, window handles, or
a force flag. `close_app` is equivalent to asking the application's windows to
close normally, so save prompts and an application's refusal to close remain
authoritative. Its trusted confirmation warns about unsaved work and identifies
the exact catalog application. File Explorer may be checked with `app_status`
but cannot be closed because `explorer.exe` also hosts the Windows desktop shell.
There is no force kill, `taskkill`, PowerShell, CMD, or generic process API.

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
check app spotify
close app notepad
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
Τρέχει το Spotify;
Κλείσε το Notepad.
```

## Tests

```powershell
python -m pytest
```

The test suite uses fake assistants and a fake system launcher. It does not
actually open programs, folders, or browser windows. GUI tests run using Qt's
offscreen platform and avoid pixel-perfect assertions.

The Windows GitHub Actions workflow runs the full tests, Python compilation,
the checked-in PyInstaller build, the post-build secret scan, and uploads the
onedir artifact. It supplies no real API key and performs no microphone, tray,
or physical hotkey acceptance tests; those remain local Windows checks.

## Architecture

### Read-only UI perception v1

`ui_inspect` is a SAFE metadata query for one explicitly named existing window.
It prepares the exact top-level handle, PID, full title, and executable used by
window focus, then revalidates that identity before and after inspection. It
does not bring the window forward. A shared `comtypes` backend obtains the UI
Automation root from that handle, verifies its handle and PID, and enumerates
only supported descendants beneath it. The result includes up to 40 controls
in UIA order: control type, bounded accessible name and automation ID, enabled,
focusable, focused, and offscreen flags, plus descriptive pattern capabilities.
Decorative controls are omitted and truncation is marked. Password controls use
a generic label. Values, document text, selected text, child handles, runtime
IDs, and coordinates are never read or returned. This tool has no UI action APIs.

### Focused keyboard input v1.1

`window_input` takes an existing window `query`, a fixed `action`, and an optional
`value` only for text actions. Every invocation is SENSITIVE and requires confirmation.
Examples: `Γράψε hello world στο Notepad.`, `Grapse hello sto Notepad.`,
`Πάτα Ctrl+S στο VS Code.`, and `Press Ctrl+L in Chrome.` These requests use provider
routing; no general keyboard-language parser is installed.

Supported actions are `type_text`, `type_text_and_enter`, `enter`, `escape`, `tab`,
`backspace`, `delete`, `arrow_up`, `arrow_down`, `arrow_left`, `arrow_right`, `home`,
`end`, `page_up`, `page_down`, `ctrl_a`, `ctrl_c`, `ctrl_v`, `ctrl_s`, `ctrl_f`,
`ctrl_l`, `ctrl_z`, and `ctrl_y`. Text must contain 1–2000 Unicode characters;
whitespace is preserved. Other actions reject `value` entirely. The strict API
transport represents omission as `null` and removes that sentinel before local
validation, following the [OpenAI function-calling contract](https://developers.openai.com/api/docs/guides/function-calling#strict-mode).

Preparation takes a fresh visible-window snapshot and uses `match_window_for_focus()`
with its existing ambiguity rules. A frozen `PreparedWindowInput` captures the exact
handle, PID, full title, executable, action, and text. Confirmation shows the target,
action, and a bounded escaped text preview, without internal identifiers. Confirm
executes that payload without another provider call or window search; Cancel sends
no input. Full text is excluded from logging and result details.

`WindowsInputController` uses only ctypes and Win32 `SendInput`: UTF-16 Unicode
down/up events for text, and private fixed virtual-key mappings for keys/shortcuts.
Execution revalidates the exact handle, PID, full title and executable, restores a
minimized target, and requires `SetForegroundWindow` to succeed. It verifies the
foreground handle and identity again immediately before the bounded event batch.
For text actions, a scoped Windows UI Automation resolver searches only descendants
of that top-level HWND. It selects one enabled, keyboard-focusable, editable
Document or Edit control in the same process, preferring Document when clearly
available. A writable Value pattern or editable Text/TextEdit pattern is required.
It calls `SetFocus` on the selected control, then checks the focused UIA element's
ancestry, PID and keyboard focus together with the original top-level foreground
identity before `SendInput`. Missing or ambiguous controls fail without input.
Other keyboard actions keep their top-level focus behavior. The packaged build
includes the `comtypes` UI Automation typelib wrappers.
Partial delivery fails without replaying input and attempts to release any keys
left down; failed cleanup is reported. Tests inject the sender and never type into
the real desktop.

Windows does not provide an atomic focus-check-and-input operation: focus can still
change after validation, and an already-held physical modifier can affect input.
`SendInput` acceptance reports inserted events, not application-level completion;
some elevated applications refuse input. See [Microsoft's SendInput documentation](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput).
There are no arbitrary shortcut strings/codes, Win-key or Alt+F4 actions, mouse
operations, clipboard APIs, keyboard hooks, shell, or subprocess additions.

```text
src/desktop_assistant/
  assistant.py    Application-facing orchestrator
  confirmation.py In-memory exact-action confirmation manager
  filesystem.py  Shared path validation and protected-location policy
  filesystem_tools.py Safe queries and confirmed filesystem mutations
  app_tools.py   Allowlisted status and confirmed graceful-close tools
  process_control.py Narrow Toolhelp/EnumWindows/WM_CLOSE boundary
  window_input.py Confirmed target-bound Unicode and fixed-key SendInput boundary
  editable_controls.py Scoped Windows UI Automation editor focus for text actions
  ui_perception.py Read-only scoped UI Automation metadata inspection
  bootstrap.py    Shared production composition for CLI and GUI
  router.py       Deterministic command parsing and dispatch
  tool_registry.py Authoritative schemas, validation, safety, and execution
  tools.py        Explicit open-app, open-folder, and open-website tools
  launcher.py     Windows-only operating-system boundary
  config.py       Central application allowlist and environment settings
  runtime_paths.py Source/frozen configuration and writable-data locations
  logging_setup.py Console-safe source logs and rotating packaged file logs
  models.py       Risk levels, interaction responses, and structured tool results
  safety.py       ALLOW / REQUIRE_CONFIRMATION / DENY policy decisions
  cli.py          Interactive command-line loop
  gui/            Native Qt window, lifecycle/tray, hotkey, single-instance IPC, workers, audio, and stylesheet
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
reparse-free source and destination-parent chains, source identity, destination
absence, destination-parent identity, operation semantics, and volume. Rename
and move use `os.rename` on the Windows target and never use `shutil.move`, so
cross-volume copy-plus-delete behavior is refused. Unexpected errors or missing
Windows reparse metadata fail closed for sensitive validation. These checks
reduce, but cannot completely eliminate, Windows filesystem TOCTOU races between
the final validation and the operating-system mutation.

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

1. Validate and sign a release build, then design a proper Windows installer.
2. Add explicit, opt-in startup-with-Windows only after installer behavior is stable.
3. Design deletion separately with recycle-bin semantics and stronger destructive confirmation.
4. Improve full-application localization while keeping transcripts faithful.

Current packaging limitations are deliberate: there is no installer, code
signing, auto-update, startup registration, microphone permission onboarding,
or one-file build. Antivirus reputation can vary for unsigned PyInstaller
executables. Windows tray/hotkey/microphone behavior must be smoke-tested on the
target machine because headless CI cannot reproduce a user's desktop session.

Confirmation reduces authorization ambiguity but cannot eliminate every external
time-of-check/time-of-use change. Future filesystem mutation tools must repeat
relevant safety checks immediately before their side effect.
