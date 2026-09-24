# Agent Instructions

Operational guidelines and durable architectural rules for coding agents working on `tasosbeast/AiProject`.

## Project Purpose
- Windows personal AI desktop assistant.
- The model proposes intent only; trusted local code validates and executes all actions.
- Prefer explicit, deterministic, narrowly scoped capabilities over open-ended automation.
- Never weaken safety guarantees or validation just to make a feature easier to implement.

## Architecture & Safety Rules
- Reuse existing core abstractions (`ToolRegistry`, `PreparedAction`, `ConfirmationManager`, `WindowController`, UI Automation backend, and validators) instead of creating parallel systems.
- `RiskLevel.SAFE` actions must remain strictly read-only and non-mutating.
- `RiskLevel.SENSITIVE` and destructive actions must go through the existing explicit confirmation flow.
- Freeze prepared targets into immutable payloads before confirmation; always revalidate targets before mutation.
- Never accept HWND, PID, RuntimeId, raw key codes, shell commands, UIA pattern IDs, or arbitrary executable paths from model input or schemas.
- Never introduce arbitrary PowerShell, `cmd.exe`, or shell execution.
- Avoid blind screen coordinates or mouse simulation when a semantic Windows or UI Automation solution exists.
- Never expose secrets, credentials, or internal identifiers (HWND, PID, RuntimeId) in schemas, logs, confirmations, or tool results.

## Development Workflow
- Inspect only files directly relevant to the requested task; expand scope only when forced by dependencies.
- Keep each task tightly focused; do not perform unrelated cleanup or refactoring.
- Prefer the smallest correct change over broad architectural changes.
- Reuse established patterns and existing helper modules before introducing new ones.
- Run targeted tests first during implementation.
- For code changes, run the full pytest suite (`.\.venv\Scripts\python.exe -m pytest -vv -ra -W error`) exactly once after targeted tests pass.
- Run `.\.venv\Scripts\python.exe -m compileall -q src tests packaging scripts` after code changes.
- Documentation-only tasks do not require pytest or compileall runs unless executable config is modified.
- Do not rerun the full test suite repeatedly.
- Do not run PyInstaller locally unless the task explicitly concerns packaging.
- Do not poll GitHub Actions; remote CI review is handled independently after pushing.
- Never claim manual GUI or hardware acceptance. The user performs real manual acceptance.

## Current Manual-Development Convention
- Manual feature acceptance is currently done from source:
  `PYTHONPATH=src ./.venv/Scripts/python.exe -m desktop_assistant.gui`
- Packaged artifact acceptance is deferred unless the task specifically targets packaging, installer, or update mechanics.
- If dependencies in `pyproject.toml` change, explicitly note that the local virtual environment must be resynced.

## Testing & Reporting Standards
- Unit and integration tests must not mutate the developer desktop; use fakes and mocks for all Windows actions.
- Never fabricate test outcomes, command outputs, or manual results.
- Keep final reports terse and structured: SHA, files changed, key changes, targeted/full test results (if applicable), compile result (if applicable), and working tree status.
- Commit and push exactly one focused commit per task.

## Scope Hygiene
- Do not commit transient milestone status, current bugs, temporary notes, or commit SHAs into `AGENTS.md`.
- `README.md` is user-facing documentation; `AGENTS.md` is strictly durable coding-agent guidance.
