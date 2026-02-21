"""CLI entry point with subcommands for querying and daemon management."""

import argparse
import json
import os
import sys
import time
from datetime import datetime

from claude_status.daemon import (
    DEFAULT_INTERVAL,
    get_daemon_status,
    poll_once,
    start_daemon,
    stop_daemon,
)
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
        f"  {'STATE':<8} {'NAME':<24} {'PROJECT':<36} {'TMUX':<8} LAST ACTIVE"
    )
    for row in rows:
        state = row["state"] or ""
        name = row["custom_title"] or row["slug"] or row["first_prompt"] or row["session_id"][:12]
        project = shorten_path(row["project_path"])
        tmux = row["tmux_target"] or ""
        last_active = ""
        if row["debug_mtime"]:
            last_active = human_relative(row["debug_mtime"])
        elif row["modified_at"]:
            last_active = human_relative(row["modified_at"])

        line = f"  {state:<8} {truncate(name, 24):<24} "
        line += f"{truncate(project, 36):<36} {tmux:<8} {last_active}"
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
        if d.get("debug_mtime"):
            print(f"  Last activity: {human_relative(d['debug_mtime'])}")


def cmd_daemon(args: argparse.Namespace) -> None:
    """Handle the daemon subcommand."""
    action = args.daemon_action

    if action == "start":
        start_daemon(interval=args.interval, foreground=args.foreground)
    elif action == "stop":
        if stop_daemon():
            print("Daemon stopped")
        else:
            print("Daemon is not running", file=sys.stderr)
            sys.exit(1)
    elif action == "status":
        status = get_daemon_status()
        if status["running"]:
            print(f"  Running (PID {status['pid']})")
            if status["last_poll"]:
                print(f"  Last poll: {status['last_poll']}")
        else:
            print("  Not running")
    elif action == "poll":
        poll_once()
        print("Poll complete")


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
    p_list.add_argument(
        "--state", "-s", choices=["active", "idle", "inactive"], help="Filter by state",
    )
    p_list.add_argument("--json", action="store_true", help="JSON output")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subparsers.add_parser("show", help="Show session details")
    p_show.add_argument("session_id", help="Session ID (full or partial)")
    p_show.add_argument("--json", action="store_true", help="JSON output")
    p_show.set_defaults(func=cmd_show)

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Manage the background daemon")
    p_daemon.set_defaults(func=cmd_daemon)
    daemon_sub = p_daemon.add_subparsers(dest="daemon_action")
    daemon_sub.required = True

    p_start = daemon_sub.add_parser("start", help="Start the daemon")
    p_start.add_argument(
        "--interval", "-i", type=int, default=DEFAULT_INTERVAL,
        help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    p_start.add_argument(
        "--foreground", "-f", action="store_true",
        help="Run in the foreground (don't daemonize)",
    )

    daemon_sub.add_parser("stop", help="Stop the daemon")
    daemon_sub.add_parser("status", help="Check daemon status")
    daemon_sub.add_parser("poll", help="Run a single poll iteration")

    # db
    p_db = subparsers.add_parser("db", help="Print the database path")
    p_db.set_defaults(func=cmd_db)

    args = parser.parse_args()

    if args.command is None:
        # Default: list active sessions
        args.all = False
        args.project = None
        args.state = None
        args.json = False
        cmd_list(args)
    elif hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
