"""Background daemon that polls session state and updates the database."""

import os
import signal
import sys
import time
from pathlib import Path

from claude_status.db import (
    get_connection,
    get_db_path,
    init_schema,
    remove_stale_runtime,
    update_meta,
)
from claude_status.scanner import scan_runtime, scan_sessions

PID_FILE = Path.home() / ".claude" / "claude-status-daemon.pid"
DEFAULT_INTERVAL = 3


def poll_once(db_path: Path | None = None) -> None:
    """Execute a single poll iteration."""
    conn = get_connection(db_path)
    try:
        init_schema(conn)
        scan_sessions(conn)
        active_ids = scan_runtime(conn)
        remove_stale_runtime(conn, active_ids)
        update_meta(conn, "last_poll", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        conn.commit()
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
            last_poll = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_poll'"
            ).fetchone()
            if last_poll:
                last_poll = last_poll["value"]
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
