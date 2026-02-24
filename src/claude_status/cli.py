"""CLI entry point with subcommands for querying session state."""

import argparse
import json
import os
import sys
import time
from datetime import datetime

from claude_status.daemon import handle_notify, poll_once
from claude_status.db import (
    get_active_sessions,
    get_all_sessions,
    get_connection,
    get_db_path,
    get_session,
    init_schema,
)


def format_ts(ts_str: str) -> str:
    """Format ISO timestamp for display in local time."""
    if not ts_str:
        return ""
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_str)
        dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts_str


def human_relative(ts: str | float | None) -> str:
    """Convert a timestamp or epoch to a human-readable relative time."""
    if ts is None:
        return ""
    if isinstance(ts, str):
        ts = ts.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
            epoch = dt.timestamp()
        except (ValueError, TypeError):
            return ""
    else:
        epoch = ts

    diff = time.time() - epoch
    if diff < 0:
        return "just now"
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


def truncate(text: str | None, width: int) -> str:
    """Truncate text to width with ellipsis."""
    if not text:
        return ""
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[:width - 1] + "\u2026"


def shorten_path(path: str | None) -> str:
    """Shorten a filesystem path for display."""
    if not path:
        return ""
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {k: row[k] for k in row.keys()}


def cmd_list(args: argparse.Namespace) -> None:
    """Handle the list subcommand."""
    conn = get_connection()
    init_schema(conn)

    if args.all or args.project or args.state:
        rows = get_all_sessions(conn, project_filter=args.project, state_filter=args.state)
    else:
        rows = get_active_sessions(conn)

    conn.close()

    if args.name:
        rows = [
            r for r in rows
            if args.name in (r["custom_title"] or "") or args.name in (r["slug"] or "")
        ]

    if not rows:
        if args.json:
            print("[]")
        else:
            label = "active sessions" if not (args.all or args.state) else "sessions"
            print(f"No {label} found.", file=sys.stderr)
        return

    if args.json:
        print(json.dumps([_row_to_dict(r) for r in rows], indent=2))
        return

    # Table output
    print(
        f"  {'STATE':<8} {'NAME':<24} {'PROJECT':<36} {'TMUX':<8} {'LAST ACTIVE':<12} SESSION ID"
    )
    for row in rows:
        state = row["state"] or ""
        name = row["custom_title"] or row["slug"] or row["first_prompt"] or row["session_id"][:12]
        project = shorten_path(row["project_path"])
        tmux = row["tmux_target"] or ""
        last_active = ""
        if row["last_activity"]:
            last_active = human_relative(row["last_activity"])
        elif row["modified_at"]:
            last_active = human_relative(row["modified_at"])
        session_id = row["session_id"]

        line = f"  {state:<8} {truncate(name, 24):<24} "
        line += f"{truncate(project, 36):<36} {tmux:<8} {last_active:<12} {session_id}"
        print(line)

    print(f"\n  {len(rows)} session(s)")


def cmd_show(args: argparse.Namespace) -> None:
    """Handle the show subcommand."""
    conn = get_connection()
    init_schema(conn)
    row = get_session(conn, args.session_id)
    conn.close()

    if row is None:
        print(f"No session found matching '{args.session_id}'", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(_row_to_dict(row), indent=2))
        return

    d = _row_to_dict(row)
    print(f"  Session:       {d['session_id']}")
    if d.get("custom_title"):
        print(f"  Title:         {d['custom_title']}")
    if d.get("slug"):
        print(f"  Slug:          {d['slug']}")
    if d.get("project_path"):
        print(f"  Project:       {shorten_path(d['project_path'])}")
    if d.get("cwd"):
        print(f"  CWD:           {shorten_path(d['cwd'])}")
    if d.get("git_branch"):
        print(f"  Branch:        {d['git_branch']}")
    if d.get("first_prompt"):
        print(f"  First prompt:  {truncate(d['first_prompt'], 80)}")
    if d.get("message_count"):
        print(f"  Messages:      {d['message_count']}")
    if d.get("is_sidechain"):
        print("  Sidechain:     yes")
    if d.get("created_at"):
        print(f"  Created:       {format_ts(d['created_at'])}")
    if d.get("modified_at"):
        print(f"  Modified:      {format_ts(d['modified_at'])}")

    # Runtime info
    if d.get("state"):
        print()
        print(f"  State:         {d['state']}")
        if d.get("pid"):
            print(f"  PID:           {d['pid']}")
        if d.get("tty"):
            print(f"  TTY:           {d['tty']}")
        if d.get("tmux_target"):
            print(f"  Tmux:          {d['tmux_target']}")
        if d.get("resume_arg"):
            print(f"  Resume arg:    {d['resume_arg']}")
        if d.get("last_activity"):
            print(f"  Last activity: {human_relative(d['last_activity'])}")


def cmd_poll(_args: argparse.Namespace) -> None:
    """Handle the poll subcommand."""
    poll_once()
    print("Poll complete")


def cmd_notify(_args: argparse.Namespace) -> None:
    """Handle the notify subcommand (hook integration)."""
    handle_notify()


def cmd_db(args: argparse.Namespace) -> None:
    """Print the database path."""
    print(get_db_path())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-status",
        description="Real-time status tracking for Claude Code sessions",
    )
    subparsers = parser.add_subparsers(dest="command")

    # list (also the default)
    p_list = subparsers.add_parser("list", help="List sessions")
    p_list.add_argument(
        "--all", "-a", action="store_true", help="Show all sessions, not just active",
    )
    p_list.add_argument("--project", "-p", help="Filter by project path (substring)")
    p_list.add_argument("--name", "-n", help="Filter by name (substring match on title/slug)")
    p_list.add_argument(
        "--state", "-s", choices=["working", "idle", "waiting", "inactive"],
        help="Filter by state",
    )
    p_list.add_argument("--json", action="store_true", help="JSON output")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subparsers.add_parser("show", help="Show session details")
    p_show.add_argument("session_id", help="Session ID (full or partial)")
    p_show.add_argument("--json", action="store_true", help="JSON output")
    p_show.set_defaults(func=cmd_show)

    # poll
    p_poll = subparsers.add_parser(
        "poll", help="Run a single poll iteration (debug/bootstrap)",
    )
    p_poll.set_defaults(func=cmd_poll)

    # notify
    p_notify = subparsers.add_parser("notify", help="Process a hook event from stdin")
    p_notify.set_defaults(func=cmd_notify)

    # db
    p_db = subparsers.add_parser("db", help="Print the database path")
    p_db.set_defaults(func=cmd_db)

    args = parser.parse_args()

    if args.command is None:
        # Default: list active sessions
        args.all = False
        args.project = None
        args.name = None
        args.state = None
        args.json = False
        cmd_list(args)
    elif hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
