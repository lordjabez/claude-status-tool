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
    get_meta,
    init_schema,
    remove_stale_runtime,
    update_meta,
    upsert_runtime_state,
    upsert_session,
)
from claude_status.scanner import scan_runtime, scan_sessions

NOTIFY_PORT = 25283
_SCAN_THROTTLE_SECONDS = 1.0


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


def _inherit_title_from_cwd(
    conn: sqlite3.Connection, session_id: str, cwd: str,
) -> None:
    """Copy custom_title from the most recent session sharing this CWD.

    After /clear, the new session has no title, slug, or first_prompt.
    Rather than waiting for scan_runtime to run _inherit_metadata (which
    depends on resume_arg being populated), look up the previous session
    by CWD and copy the title immediately so the session is displayable.
    """
    row = conn.execute(
        """SELECT custom_title, project_path, project_dir
           FROM sessions
           WHERE (cwd = ? OR project_path = ?)
             AND session_id != ?
             AND custom_title IS NOT NULL
           ORDER BY modified_at DESC LIMIT 1""",
        (cwd, cwd, session_id),
    ).fetchone()
    if row:
        upsert_session(conn, {
            "session_id": session_id,
            "custom_title": row["custom_title"],
            "project_path": row["project_path"],
            "project_dir": row["project_dir"],
        })


def _get_current_state(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Return the current runtime state for a session, or None if no row exists."""
    row = conn.execute(
        "SELECT state FROM runtime WHERE session_id = ?", (session_id,),
    ).fetchone()
    return row["state"] if row else None


def _process_hook_event(conn: sqlite3.Connection, payload: dict) -> bool:
    """Dispatch a hook event to the appropriate DB update.

    Returns True if a consumer-visible change occurred (state transition,
    session added/removed, or a scan ran that may have updated metadata).
    """
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    if not session_id:
        return False

    changed = False
    prev_state = _get_current_state(conn, session_id)

    if event == "SessionStart":
        cwd = payload.get("cwd")
        upsert_session(conn, {"session_id": session_id, "cwd": cwd})
        upsert_runtime_state(conn, session_id, "idle")
        # After /clear, the new session has no title/slug yet. Copy the title
        # from the most recent session in the same project directory so the
        # session is immediately displayable (don't wait for scan_runtime).
        if cwd:
            _inherit_title_from_cwd(conn, session_id, cwd)
        changed = True  # New session always matters.

    elif event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "TaskCompleted"):
        upsert_session(conn, {"session_id": session_id, "cwd": payload.get("cwd")})
        upsert_runtime_state(conn, session_id, "working", time.time())
        changed = prev_state != "working"

    elif event == "PermissionRequest":
        try:
            upsert_runtime_state(conn, session_id, "waiting")
            changed = prev_state != "waiting"
        except sqlite3.IntegrityError:
            pass  # Session row doesn't exist yet; nothing to update.

    elif event == "Stop":
        # Don't override "waiting" — the user hasn't responded to the
        # permission/elicitation prompt yet.  PostToolUse will clear it.
        if prev_state != "waiting":
            try:
                upsert_runtime_state(conn, session_id, "idle")
                changed = prev_state != "idle"
            except sqlite3.IntegrityError:
                pass  # Session row doesn't exist yet; nothing to update.

    elif event == "Notification":
        ntype = payload.get("notification_type")
        if ntype in ("permission_prompt", "elicitation_dialog"):
            try:
                upsert_runtime_state(conn, session_id, "waiting")
                changed = prev_state != "waiting"
            except sqlite3.IntegrityError:
                pass

    elif event == "SessionEnd":
        delete_runtime(conn, session_id)
        changed = prev_state is not None  # Only matters if a row existed.

    # Throttled full scan: refresh session catalog, process info, and stale cleanup.
    # State updates above are always immediate; the scan is the expensive part
    # (subprocess calls to ps/lsof/tmux), so skip it if one ran recently.
    # A scan may update metadata or clean up stale rows, so always notify after one.
    last_scan = get_meta(conn, "last_scan")
    now = time.time()
    if last_scan is None or now - float(last_scan) >= _SCAN_THROTTLE_SECONDS:
        scan_sessions(conn)
        active_ids = scan_runtime(conn, detect_states=False)
        # Protect the current session from stale cleanup — the process may not
        # be visible to ps yet, or _resolve_session_id may not map it correctly.
        active_ids.add(session_id)
        remove_stale_runtime(conn, active_ids)
        update_meta(conn, "last_scan", str(now))
        changed = True

    return changed


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
            changed = _process_hook_event(conn, payload)
            conn.commit()
            if changed:
                _notify_udp()
        finally:
            conn.close()
    except Exception:
        pass  # Never break Claude.
