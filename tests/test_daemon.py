"""Tests for claude_status.daemon hook-notify functionality."""

import json
import time
from pathlib import Path
from unittest.mock import patch

from claude_status.daemon import _process_hook_event, handle_notify
from claude_status.db import get_connection, init_schema, upsert_runtime, upsert_session


def _make_db(tmp_path: Path):
    conn = get_connection(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_session_start_sets_idle(tmp_path):
    conn = _make_db(tmp_path)

    with patch("claude_status.daemon.scan_sessions"), \
         patch("claude_status.daemon.scan_runtime", return_value=set()), \
         patch("claude_status.daemon.remove_stale_runtime"):
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-start",
            "cwd": "/tmp/project",
        }
        _process_hook_event(conn, payload)
        conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-start'").fetchone()
    assert row is not None
    assert row["state"] == "idle"

    # Session row should also exist.
    srow = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess-start'").fetchone()
    assert srow is not None
    assert srow["cwd"] == "/tmp/project"
    conn.close()


def test_user_prompt_submit_creates_working_state(tmp_path):
    conn = _make_db(tmp_path)

    with patch("claude_status.daemon.scan_sessions"), \
         patch("claude_status.daemon.scan_runtime", return_value=set()), \
         patch("claude_status.daemon.remove_stale_runtime"):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-1",
            "cwd": "/tmp/project",
        }
        _process_hook_event(conn, payload)
        conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-1'").fetchone()
    assert row is not None
    assert row["state"] == "working"
    assert row["last_activity"] is not None
    conn.close()


def test_post_tool_use_sets_working(tmp_path):
    conn = _make_db(tmp_path)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-2",
    }
    _process_hook_event(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-2'").fetchone()
    assert row["state"] == "working"
    conn.close()


def test_stop_sets_idle(tmp_path):
    conn = _make_db(tmp_path)
    # First create the session and runtime row.
    upsert_session(conn, {"session_id": "sess-3"})
    upsert_runtime(conn, {"session_id": "sess-3", "pid": 1234, "state": "working"})
    conn.commit()

    payload = {"hook_event_name": "Stop", "session_id": "sess-3"}
    _process_hook_event(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-3'").fetchone()
    assert row["state"] == "idle"
    # pid should be preserved (not wiped by the state-only upsert).
    assert row["pid"] == 1234
    conn.close()


def test_stop_ignores_missing_session(tmp_path):
    """Stop for a session that doesn't exist yet should not raise."""
    conn = _make_db(tmp_path)
    payload = {"hook_event_name": "Stop", "session_id": "nonexistent"}
    _process_hook_event(conn, payload)  # Should not raise
    conn.commit()
    conn.close()


def test_notification_permission_prompt_sets_waiting(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "sess-4"})
    upsert_runtime(conn, {"session_id": "sess-4", "state": "working"})
    conn.commit()

    payload = {
        "hook_event_name": "Notification",
        "session_id": "sess-4",
        "notification_type": "permission_prompt",
    }
    _process_hook_event(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-4'").fetchone()
    assert row["state"] == "waiting"
    conn.close()


def test_notification_other_type_ignored(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "sess-5"})
    upsert_runtime(conn, {"session_id": "sess-5", "state": "working"})
    conn.commit()

    payload = {
        "hook_event_name": "Notification",
        "session_id": "sess-5",
        "notification_type": "something_else",
    }
    _process_hook_event(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-5'").fetchone()
    assert row["state"] == "working"  # Unchanged
    conn.close()


def test_session_end_deletes_runtime(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "sess-6"})
    upsert_runtime(conn, {"session_id": "sess-6", "state": "idle"})
    conn.commit()

    payload = {"hook_event_name": "SessionEnd", "session_id": "sess-6"}
    _process_hook_event(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-6'").fetchone()
    assert row is None
    conn.close()


def test_missing_session_id_is_noop(tmp_path):
    """Payload without session_id should do nothing."""
    conn = _make_db(tmp_path)
    payload = {"hook_event_name": "UserPromptSubmit"}
    _process_hook_event(conn, payload)
    conn.commit()

    rows = conn.execute("SELECT * FROM runtime").fetchall()
    assert len(rows) == 0
    conn.close()


def test_unknown_event_is_noop(tmp_path):
    conn = _make_db(tmp_path)
    payload = {"hook_event_name": "SomeFutureEvent", "session_id": "sess-7"}
    _process_hook_event(conn, payload)
    conn.commit()
    conn.close()


def test_working_preserves_daemon_fields(tmp_path):
    """Hook state update should not wipe pid/tty/tmux set by process scan."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "sess-8"})
    upsert_runtime(conn, {
        "session_id": "sess-8",
        "pid": 9999,
        "tty": "ttys003",
        "tmux_target": "main:1.0",
        "state": "idle",
    })
    conn.commit()

    with patch("claude_status.daemon.scan_sessions"), \
         patch("claude_status.daemon.scan_runtime", return_value=set()), \
         patch("claude_status.daemon.remove_stale_runtime"):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "sess-8"}
        _process_hook_event(conn, payload)
        conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-8'").fetchone()
    assert row["state"] == "working"
    assert row["pid"] == 9999
    assert row["tty"] == "ttys003"
    assert row["tmux_target"] == "main:1.0"
    conn.close()


def test_handle_notify_reads_stdin(tmp_path):
    """handle_notify should read JSON from stdin and update the DB."""
    db_path = tmp_path / "test.db"
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "notify-test",
        "cwd": "/tmp",
    })

    with (
        patch("claude_status.db.get_db_path", return_value=db_path),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.read.return_value = payload
        handle_notify()

    conn = get_connection(db_path)
    init_schema(conn)
    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'notify-test'").fetchone()
    assert row is not None
    assert row["state"] == "working"
    conn.close()


def test_handle_notify_bad_json_does_not_raise():
    """Malformed input should be silently swallowed."""
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.read.return_value = "not json at all"
        handle_notify()  # Should not raise


def test_last_activity_updates_on_post_tool_use(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "sess-9"})
    upsert_runtime(conn, {"session_id": "sess-9", "state": "working", "last_activity": 1000.0})
    conn.commit()

    before = time.time()
    payload = {"hook_event_name": "PostToolUse", "session_id": "sess-9"}
    _process_hook_event(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'sess-9'").fetchone()
    assert row["last_activity"] >= before
    conn.close()


def test_full_scan_events_trigger_scans(tmp_path):
    """SessionStart and UserPromptSubmit should trigger full scans."""
    conn = _make_db(tmp_path)

    for event in ("SessionStart", "UserPromptSubmit"):
        with patch("claude_status.daemon.scan_sessions") as mock_scan_sess, \
             patch("claude_status.daemon.scan_runtime",
                   return_value=set()) as mock_scan_rt, \
             patch("claude_status.daemon.remove_stale_runtime") as mock_stale:
            payload = {
                "hook_event_name": event,
                "session_id": f"scan-{event}",
                "cwd": "/tmp",
            }
            _process_hook_event(conn, payload)
            conn.commit()

            mock_scan_sess.assert_called_once_with(conn)
            mock_scan_rt.assert_called_once_with(conn, detect_states=False)
            mock_stale.assert_called_once()

    conn.close()


def test_lightweight_events_skip_scans(tmp_path):
    """PostToolUse, Stop, Notification, and SessionEnd should not trigger full scans."""
    conn = _make_db(tmp_path)

    # Set up session/runtime rows for events that need them.
    for sid in ("light-PostToolUse", "light-Stop", "light-Notification", "light-SessionEnd"):
        upsert_session(conn, {"session_id": sid})
        upsert_runtime(conn, {"session_id": sid, "state": "working"})
    conn.commit()

    lightweight_events = [
        {"hook_event_name": "PostToolUse", "session_id": "light-PostToolUse"},
        {"hook_event_name": "Stop", "session_id": "light-Stop"},
        {
            "hook_event_name": "Notification",
            "session_id": "light-Notification",
            "notification_type": "permission_prompt",
        },
        {"hook_event_name": "SessionEnd", "session_id": "light-SessionEnd"},
    ]

    for payload in lightweight_events:
        with patch("claude_status.daemon.scan_sessions") as mock_scan_sess, \
             patch("claude_status.daemon.scan_runtime") as mock_scan_rt:
            _process_hook_event(conn, payload)
            conn.commit()

            mock_scan_sess.assert_not_called()
            mock_scan_rt.assert_not_called()

    conn.close()
