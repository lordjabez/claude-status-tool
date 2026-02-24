"""Background daemon that polls session state and updates the database."""

import json
import os
import signal
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_status.db import (
    delete_runtime,
    get_connection,
    get_db_path,
    get_meta,
    init_schema,
    remove_stale_runtime,
    update_meta,
    upsert_runtime_state,
    upsert_session,
)
from claude_status.scanner import scan_runtime, scan_sessions

PID_FILE = Path.home() / ".claude" / "claude-status-daemon.pid"
DEFAULT_INTERVAL = 10
NOTIFY_PORT = 25283


def _notify_udp() -> None:
    """Send an empty UDP datagram to signal the Logi Options+ plugin to poll."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"", ("127.0.0.1", NOTIFY_PORT))
    except OSError:
        pass


def poll_once(db_path: Path | None = None) -> None:
    """Execute a single poll iteration."""
    poll_started = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        scan_sessions(conn)
        active_ids = scan_runtime(conn)

        # Hooks may have created runtime rows that the daemon can't resolve
        # back to a ps process (e.g. the session just started).  Keep any
        # runtime row whose updated_at is >= the start of this poll cycle —
        # a hook wrote it recently and the next poll will reconcile.
        hook_ids = {
            r["session_id"] for r in conn.execute(
                "SELECT session_id FROM runtime WHERE updated_at >= ?",
                (poll_started,),
            ).fetchall()
        }
        remove_stale_runtime(conn, active_ids | hook_ids)

        update_meta(conn, "last_poll", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        conn.commit()
        _notify_udp()
    finally:
        conn.close()


def _read_pid_file() -> int | None:
    """Read the PID from the PID file, if it exists."""
    try:
        pid_str = PID_FILE.read_text().strip()
        return int(pid_str)
    except (OSError, ValueError):
        return None


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_daemon_status() -> dict:
    """Check daemon status. Returns dict with 'running', 'pid', 'last_poll'."""
    pid = _read_pid_file()
    if pid is not None and _is_process_running(pid):
        # Check last poll time from DB
        last_poll = None
        try:
            conn = get_connection()
            last_poll = get_meta(conn, "last_poll")
            conn.close()
        except Exception:
            pass
        return {"running": True, "pid": pid, "last_poll": last_poll}

    # PID file exists but process is gone - clean up
    if pid is not None:
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    return {"running": False, "pid": None, "last_poll": None}


def start_daemon(interval: int = DEFAULT_INTERVAL, foreground: bool = False) -> None:
    """Start the daemon process."""
    status = get_daemon_status()
    if status["running"]:
        print(f"Daemon already running (PID {status['pid']})", file=sys.stderr)
        sys.exit(1)

    if not foreground:
        # Fork into background
        pid = os.fork()
        if pid > 0:
            # Parent: wait briefly for child to write PID file
            time.sleep(0.3)
            if PID_FILE.exists():
                child_pid = _read_pid_file()
                print(f"Daemon started (PID {child_pid})")
            else:
                print("Daemon started")
            return
        # Child: create new session
        os.setsid()
        # Redirect stdio
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)

    # Write PID file
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    db_path = get_db_path()

    try:
        while not shutdown:
            try:
                poll_once(db_path)
            except Exception:
                pass  # Keep running on transient errors
            # Sleep in small increments so we respond to signals quickly
            for _ in range(interval * 10):
                if shutdown:
                    break
                time.sleep(0.1)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def stop_daemon() -> bool:
    """Stop the running daemon. Returns True if stopped successfully."""
    pid = _read_pid_file()
    if pid is None:
        return False

    if not _is_process_running(pid):
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return False

    os.kill(pid, signal.SIGTERM)

    # Wait for process to exit
    for _ in range(30):
        if not _is_process_running(pid):
            return True
        time.sleep(0.1)

    # Force kill if still running
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return True


def _process_hook_event(conn: sqlite3.Connection, payload: dict) -> None:
    """Dispatch a hook event to the appropriate DB update."""
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id")
    if not session_id:
        return

    if event in ("UserPromptSubmit", "PostToolUse"):
        # Ensure a minimal session row exists to satisfy the FK constraint.
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
