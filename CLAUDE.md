# Claude Code Instructions

## Project Overview

`claude-status` is a real-time status tracker for Claude Code sessions. Claude Code hooks
push state transitions directly to a SQLite database. A CLI and direct DB access provide
querying. A `poll` command is available for debugging and bootstrapping.

## Architecture

- **Hook-driven**: hooks are the sole source of state; no background polling
- **Single writer per event** (notify command), **multiple readers** (CLI, scripts, dashboards) via SQLite WAL mode
- Zero external Python dependencies; stdlib only
- Database at `~/.claude/claude-status.db` (override with `CLAUDE_STATUS_DB` env var)

## Module Layout

```text
src/claude_status/
  db.py        # SQLite schema, connection (WAL), upsert/query helpers
  process.py   # ps parsing, lsof CWD lookup, tmux mapping, JSONL-based state detection
  scanner.py   # Session catalog scan (index + JSONL fallback), session ID resolution, runtime process info
  hooks.py     # Hook event dispatch, throttled full-scan, poll_once debug tool, UDP notify
  demo.py      # Demo mode: mock sessions cycling through states for testing consumers
  cli.py       # argparse CLI: list, show, poll, notify, demo, db
```

Dependency flow: `cli -> hooks, demo -> scanner -> db, process`

## Development Commands

```bash
uv sync                        # install package + dev deps
uv run ruff check src/ tests/  # lint (must pass clean)
uv run pytest -v               # run tests (must pass)
```

After editing source, if testing the installed CLI entry point, reinstall with:

```bash
uv sync --reinstall-package claude-status
```

Or use `uv run claude-status ...` which picks up edits automatically.

## Code Conventions

- Python >= 3.11, use modern type hints (`str | None`, `list[str]`)
- Ruff for linting: line length 100, rules E/F/W/I
- hatchling build backend with `src/` layout
- No external dependencies; everything uses the Python standard library
- Tests use `pytest` with temp SQLite databases; no mocking of system calls in unit tests
- Test files mirror source: `test_db.py`, `test_process.py`, `test_scanner.py`, `test_hooks.py`

## Data Sources

On hook events, the notify command reads from:

- `~/.claude/projects/*/sessions-index.json` for fast session metadata
- `~/.claude/projects/*/*.jsonl` as fallback (mtime-guarded to avoid re-parsing)
- `ps -eo pid,tty,args` + `lsof` for running process detection
- `tmux list-panes` + `tmux list-clients` for pane mapping and client TTY resolution

Every hook event updates state immediately, then runs a full scan if the last scan was more
than 1 second ago (throttled to avoid redundant subprocess calls during rapid tool-use bursts).

## Key Design Decisions

- Hooks are the sole source of state; no background polling
- JSONL files are only re-parsed when their mtime changes (stored in `jsonl_mtime` column)
- `poll` command uses JSONL mtime heuristics for state detection (debug/bootstrap only)
- `folder_label()` in `scanner.py` reconstructs filesystem paths from Claude's hyphenated
  directory names by greedy matching against existing paths on disk
- Session ID resolution for bare `claude` processes (no `--resume`) uses `lsof` to get the
  process CWD and matches it to a project path in the database
- `update_runtime_process_info()` updates pid/tty/tmux without touching state, preserving
  hook-set state values
- After `/clear` or `/compact`, process `--resume` args become stale; `scan_runtime` uses
  a PID map from existing runtime rows and a CWD fallback to match processes correctly;
  orphan sessions (no slug, no modified_at) are excluded from `pid_map` so their stale
  PID mappings don't prevent re-resolution
- After `/clear`, title inheritance happens in two layers: (1) the `SessionStart` hook
  handler immediately copies the title from the most recent session with the same CWD
  via `_inherit_title_from_cwd()`, so the session is displayable without waiting for a
  scan; (2) `_inherit_metadata()` in `scan_runtime` acts as a backup, propagating metadata
  by following the `resume_arg` UUID stored in the runtime row
- "Waiting" state is sticky: once set by a hook (PermissionRequest or
  Notification/elicitation_dialog/permission_prompt), neither Stop events nor poll-based
  JSONL detection may overwrite it with "idle". Only an explicit action event
  (UserPromptSubmit, PreToolUse, PostToolUse, etc.) clears it.
