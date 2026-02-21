# Claude Code Instructions

## Project Overview

`claude-status` is a real-time status tracker for Claude Code sessions. A background daemon
polls session data every 3 seconds and writes to a SQLite database. A CLI and direct DB
access provide querying.

## Architecture

- **Single writer** (daemon), **multiple readers** (CLI, scripts, dashboards) via SQLite WAL mode
- Zero external Python dependencies; stdlib only
- Database at `~/.claude/claude-status.db` (override with `CLAUDE_STATUS_DB` env var)
- Daemon PID file at `~/.claude/claude-status-daemon.pid`

## Module Layout

```text
src/claude_status/
  db.py        # SQLite schema, connection (WAL), upsert/query helpers
  process.py   # ps parsing, lsof CWD lookup, tmux pane mapping, debug log mtime
  scanner.py   # Session catalog scan (index + JSONL fallback), session ID resolution, state detection
  daemon.py    # Poll loop, fork/daemonize, PID file, signal handling
  cli.py       # argparse CLI: list, show, daemon {start,stop,status,poll}, db
```

Dependency flow: `cli -> daemon -> scanner -> db, process`

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
- Test files mirror source: `test_db.py`, `test_process.py`, `test_scanner.py`

## Data Sources

The daemon reads from these sources each poll cycle:

- `~/.claude/projects/*/sessions-index.json` for fast session metadata
- `~/.claude/projects/*/*.jsonl` as fallback (mtime-guarded to avoid re-parsing)
- `ps -eo pid,tty,args` + `lsof` for running process detection
- `tmux list-panes` for TTY-to-pane mapping
- `~/.claude/debug/{id}.txt` mtime for active/idle state detection

## Key Design Decisions

- JSONL files are only re-parsed when their mtime changes (stored in `jsonl_mtime` column)
- State detection: debug log mtime within 5s = "active", otherwise "idle"
- `folder_label()` in `scanner.py` reconstructs filesystem paths from Claude's hyphenated
  directory names by greedy matching against existing paths on disk
- Session ID resolution for bare `claude` processes (no `--resume`) uses `lsof` to get the
  process CWD and matches it to a project path in the database
