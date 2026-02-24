"""Hook-driven state management and poll-based debug/bootstrap tool."""

import json
import socket
import sqlite3
import sys
import time
from pathlib import Path

from claude_status.db import (
    delete_runtime,
    get_connection,
    init_schema,
    remove_stale_runtime,
    update_meta,
    upsert_runtime_state,
    upsert_session,
)
from claude_status.scanner import scan_runtime, scan_sessions

NOTIFY_PORT = 25283

# Events that trigger a full scan (session catalog + process info + stale cleanup).
_FULL_SCAN_EVENTS = {"SessionStart", "UserPromptSubmit"}


def _notify_udp() -> None:
    """Send an empty UDP datagram to signal the Logi Options+ plugin to poll."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"", ("127.0.0.1", NOTIFY_PORT))
    except OSError:
        pass


def poll_once(db_path: Path | None = None) -> None:
    """Execute a single poll iteration (debug/bootstrap tool).

    Scans session metadata, detects running processes with state inference,
    and cleans up stale runtime rows.
    """
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        scan_sessions(conn)
        active_ids = scan_runtime(conn)
        remove_stale_runtime(conn, active_ids)
        update_meta(conn, "last_poll", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        conn.commit()
        _notify_udp()
    finally:
        conn.close()


def _process_hook_event(conn: sqlite3.Connection, payload: dict) -> None:
    """Dispatch a hook event to the appropriate DB update."""
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    if not session_id:
        return

    if event == "SessionStart":
        upsert_session(conn, {"session_id": session_id, "cwd": payload.get("cwd")})
        upsert_runtime_state(conn, session_id, "idle")

    elif event in ("UserPromptSubmit", "PostToolUse"):
        upsert_session(conn, {"session_id": session_id, "cwd": payload.get("cwd")})
        upsert_runtime_state(conn, session_id, "working", time.time())

    elif event == "Stop":
        try:
            upsert_runtime_state(conn, session_id, "idle")
        except sqlite3.IntegrityError:
            pass  # Session row doesn't exist yet; nothing to update.

    elif event == "Notification":
        if payload.get("notification_type") == "permission_prompt":
            try:
                upsert_runtime_state(conn, session_id, "waiting")
            except sqlite3.IntegrityError:
                pass

    elif event == "SessionEnd":
        delete_runtime(conn, session_id)

    # Full-scan events: refresh session catalog, process info, and stale cleanup.
    if event in _FULL_SCAN_EVENTS:
        scan_sessions(conn)
        active_ids = scan_runtime(conn, detect_states=False)
        # Include the current session_id so the just-created row isn't cleaned up
        # before ps can see the process.
        active_ids.add(session_id)
        remove_stale_runtime(conn, active_ids)


def handle_notify() -> None:
    """Entry point for the ``claude-status notify`` CLI command.

    Reads a JSON object from stdin, opens the DB, dispatches the event, and
    exits.  Wraps everything in a blanket try/except so a failure here never
    blocks Claude Code (hooks run with ``"async": true``).
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        conn = get_connection()
        try:
            init_schema(conn)
            _process_hook_event(conn, payload)
            conn.commit()
            _notify_udp()
        finally:
            conn.close()
    except Exception:
        pass  # Never break Claude.
