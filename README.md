# claude-status

Real-time status tracking for Claude Code sessions. Claude Code hooks push state transitions to a SQLite database as they happen, and a CLI provides quick queries. Other tools (dashboards, status bars, scripts) can read the database directly.

## Architecture

```text
Claude Code hooks  ──┐
  (SessionStart,     │
   UserPromptSubmit, ├──▶  claude-status notify  ──▶  ~/.claude/claude-status.db
   PostToolUse,      │       (state + metadata)              │
   Stop, etc.)     ──┘                                       ▼
                                                     claude-status CLI
                                                     (or any SQLite reader)
```

Hooks are the sole source of state. The `notify` command reads hook events from stdin, updates state in the database, and on low-frequency events (SessionStart, UserPromptSubmit) runs a full scan to populate metadata (pid, tmux pane, session catalog). The database uses WAL mode so any number of readers can query concurrently without blocking.

A `poll` command is available for debugging and bootstrapping (populates the DB from scratch by scanning processes and session files).

## Requirements

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)
- No external Python dependencies

## Installation

```bash
uv tool install -e /path/to/claude-status-tool
```

This puts `claude-status` on your PATH via `~/.local/bin/`. The `-e` (editable) flag means source changes take effect without reinstalling.

To uninstall:

```bash
uv tool uninstall claude-status
```

## Hook Configuration

Add the following to `~/.claude/settings.json` to enable real-time state tracking:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": {},
        "hooks": [{ "type": "command", "command": "claude-status notify", "async": true }]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": {},
        "hooks": [{ "type": "command", "command": "claude-status notify", "async": true }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": {},
        "hooks": [{ "type": "command", "command": "claude-status notify", "async": true }]
      }
    ],
    "Stop": [
      {
        "matcher": {},
        "hooks": [{ "type": "command", "command": "claude-status notify", "async": true }]
      }
    ],
    "Notification": [
      {
        "matcher": {},
        "hooks": [{ "type": "command", "command": "claude-status notify", "async": true }]
      }
    ],
    "SessionEnd": [
      {
        "matcher": {},
        "hooks": [{ "type": "command", "command": "claude-status notify", "async": true }]
      }
    ]
  }
}
```

All hooks use `"async": true` so they never block Claude Code. The `notify` command reads JSON from stdin, updates the database, and exits silently on any error.

### Event mapping

| Hook Event         | State Action                       | Full Scan? |
| ------------------ | ---------------------------------- | ---------- |
| `SessionStart`     | state = "idle"                     | Yes        |
| `UserPromptSubmit` | state = "working"                  | Yes        |
| `PostToolUse`      | state = "working", last_activity   | No         |
| `Stop`             | state = "idle"                     | No         |
| `Notification`     | state = "waiting" (if perm prompt) | No         |
| `SessionEnd`       | delete runtime row                 | No         |

"Full scan" means the hook also refreshes the session catalog, updates pid/tty/tmux info from running processes, and cleans up stale runtime rows. PostToolUse fires many times per turn, so it only updates state to keep latency low.

## Usage

### Listing sessions

```bash
claude-status                           # list active (running) sessions
claude-status list --all                # list all sessions
claude-status list --project foo        # filter by project path substring
claude-status list --name foo           # filter by name (case-sensitive substring)
claude-status list --state working      # filter: working, idle, waiting, or inactive
claude-status list --all --json         # JSON output
```

Example output:

```text
  STATE    NAME                     PROJECT                              TMUX     LAST ACTIVE
  working  Chief of Staff Plan      ~/Projects/jud/chief-of-staff        0:0.0    2s ago
  idle     Data System              ~/Projects/jud/data-system           5:0.0    3m ago
```

### Session details

```bash
claude-status show SESSION_ID          # full or partial UUID
claude-status show abc123 --json       # JSON output
```

### Poll (debug/bootstrap)

```bash
claude-status poll                     # one-shot scan of processes + session files
```

Use `poll` to bootstrap the database before any hooks have fired, or to debug state by forcing a full scan with process-based state detection.

### Database path

```bash
claude-status db                       # prints ~/.claude/claude-status.db
```

Override with the `CLAUDE_STATUS_DB` environment variable.

## Direct database access

The database is a standard SQLite file. Any tool that reads SQLite can query it:

```bash
sqlite3 ~/.claude/claude-status.db "SELECT * FROM runtime WHERE state = 'working'"
```

### Schema

**sessions** - session metadata from index files and JSONL parsing:

| Column        | Type    | Description                           |
| ------------- | ------- | ------------------------------------- |
| session_id    | TEXT PK | UUID                                  |
| slug          | TEXT    | Auto-generated session name           |
| custom_title  | TEXT    | User-assigned name (via /rename)      |
| project_path  | TEXT    | Filesystem path to the project        |
| project_dir   | TEXT    | Claude's internal directory name      |
| cwd           | TEXT    | Working directory at session start    |
| git_branch    | TEXT    | Branch name                           |
| first_prompt  | TEXT    | First user message (truncated)        |
| message_count | INTEGER | Number of assistant messages (>= 0)   |
| is_sidechain  | INTEGER | Whether this is a sidechain session   |
| jsonl_path    | TEXT    | Path to the JSONL transcript          |
| jsonl_mtime   | REAL    | Last modification time of JSONL file  |
| created_at    | TEXT    | ISO timestamp                         |
| modified_at   | TEXT    | ISO timestamp                         |
| updated_at    | TEXT    | Last DB update                        |

**runtime** - state of currently running sessions:

| Column         | Type    | Description                         |
| -------------- | ------- | ----------------------------------- |
| session_id     | TEXT PK | FK to sessions (enforced)           |
| pid            | INTEGER | OS process ID                       |
| tty            | TEXT    | TTY device                          |
| tmux_target    | TEXT    | tmux pane (e.g. "0:0.0")            |
| tmux_session   | TEXT    | tmux session name                   |
| resume_arg     | TEXT    | Value of --resume if used           |
| state          | TEXT    | working, idle, or waiting (checked) |
| last_activity  | REAL    | Last JSONL write (epoch)            |
| updated_at     | TEXT    | Last DB update                      |

**meta** - metadata (e.g. `last_poll` timestamp).

## Development

```bash
uv sync                    # install dev dependencies
uv run ruff check src/     # lint
uv run pytest -v           # run tests
```
