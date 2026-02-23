# claude-status

Real-time status tracking for Claude Code sessions. A background daemon keeps a SQLite database continuously updated with session metadata and runtime state, and a CLI provides quick queries. Other tools (dashboards, status bars, scripts) can read the database directly.

## Architecture

```text
~/.claude/projects/*/     ──┐
*.jsonl (conversation)    ──┤
ps (claude processes)     ──┼──▶  daemon (polls every 3s)  ──▶  ~/.claude/claude-status.db
tmux list-panes           ──┘                                         │
                                                                      ▼
                                                              claude-status CLI
                                                              (or any SQLite reader)
```

The daemon is the single writer. The database uses WAL mode so any number of readers (CLI, scripts, dashboards) can query concurrently without blocking.

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

## Usage

### Daemon

The daemon polls every 3 seconds by default, scanning session files, detecting running processes, mapping tmux panes, and updating the database.

```bash
claude-status daemon start              # start in background
claude-status daemon start --interval 5 # custom poll interval (seconds)
claude-status daemon start --foreground # run in foreground (for debugging)
claude-status daemon status             # check if running, show last poll time
claude-status daemon stop               # graceful shutdown
claude-status daemon poll               # run a single poll iteration (no daemon)
```

The daemon writes a PID file to `~/.claude/claude-status-daemon.pid`.

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

**meta** - daemon metadata (e.g. `last_poll` timestamp).

## Development

```bash
uv sync                    # install dev dependencies
uv run ruff check src/     # lint
uv run pytest -v           # run tests
```
