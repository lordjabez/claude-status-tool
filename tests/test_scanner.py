"""Tests for claude_status.scanner module."""

import json
from pathlib import Path
from unittest.mock import patch

from claude_status.db import (
    get_connection,
    init_schema,
    upsert_runtime_state,
    upsert_session,
)
from claude_status.scanner import (
    _looks_like_uuid,
    _parse_jsonl,
    _propagate_titles,
    _resolve_session_id,
    _truncate,
    folder_label,
    scan_runtime,
)


def test_folder_label_basic():
    # Use paths that actually exist on the test system
    result = folder_label("-Users")
    assert result == "/Users"

    result = folder_label("-tmp")
    assert result == "/tmp"


def test_folder_label_non_hyphen():
    assert folder_label("plain-name") == "plain-name"


def test_looks_like_uuid():
    assert _looks_like_uuid("abc12345-1234-5678-9abc-def012345678")
    assert _looks_like_uuid("ABC12345-1234-5678-9ABC-DEF012345678")
    assert not _looks_like_uuid("not-a-uuid")
    assert not _looks_like_uuid("abc123")
    assert not _looks_like_uuid("")


def test_truncate():
    assert _truncate(None, 10) is None
    assert _truncate("short", 10) == "short"
    assert _truncate("a long string here", 10) == "a long st\u2026"
    assert len(_truncate("a long string here", 10)) == 10


def test_parse_jsonl(tmp_path):
    """Test JSONL parsing with synthetic data."""
    jsonl_file = tmp_path / "test.jsonl"
    entries = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "slug": "test-slug",
            "cwd": "/test",
            "message": {"content": [{"type": "text", "text": "Hello world"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:01:00Z",
        },
        {
            "type": "custom-title",
            "customTitle": "My Title",
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:02:00Z",
            "message": {"content": [{"type": "text", "text": "Second message"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:03:00Z",
        },
    ]
    jsonl_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    result = _parse_jsonl(jsonl_file)
    assert result is not None
    assert result["title"] == "My Title"
    assert result["slug"] == "test-slug"
    assert result["cwd"] == "/test"
    assert result["first_ts"] == "2026-01-01T00:00:00Z"
    assert result["last_ts"] == "2026-01-01T00:03:00Z"
    assert result["message_count"] == 2
    assert result["first_user_text"] == "Hello world"


def test_parse_jsonl_empty(tmp_path):
    """Test JSONL parsing with no valid entries."""
    jsonl_file = tmp_path / "empty.jsonl"
    jsonl_file.write_text("{}\n")

    result = _parse_jsonl(jsonl_file)
    assert result is None


def test_parse_jsonl_skips_interrupted_requests(tmp_path):
    """First user text should skip [Request interrupted...] messages."""
    jsonl_file = tmp_path / "interrupted.jsonl"
    entries = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:01:00Z",
            "message": {"content": [{"type": "text", "text": "Real prompt"}]},
        },
    ]
    jsonl_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    result = _parse_jsonl(jsonl_file)
    assert result is not None
    assert result["first_user_text"] == "Real prompt"


def _make_db(tmp_path: Path):
    conn = get_connection(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_propagate_titles(tmp_path):
    """Continued sessions with the same slug should inherit custom_title."""
    conn = _make_db(tmp_path)

    # Original session: has slug and custom_title
    upsert_session(conn, {
        "session_id": "original-1111",
        "slug": "my-cool-slug",
        "custom_title": "My Project",
        "modified_at": "2026-01-01T00:00:00Z",
    })

    # Continuation session: same slug, no custom_title
    upsert_session(conn, {
        "session_id": "continuation-2222",
        "slug": "my-cool-slug",
        "modified_at": "2026-01-02T00:00:00Z",
    })

    # Unrelated session: different slug, no title (should not be affected)
    upsert_session(conn, {
        "session_id": "unrelated-3333",
        "slug": "other-slug",
        "modified_at": "2026-01-01T00:00:00Z",
    })

    conn.commit()
    _propagate_titles(conn)
    conn.commit()

    row = conn.execute(
        "SELECT custom_title FROM sessions WHERE session_id = 'continuation-2222'"
    ).fetchone()
    assert row["custom_title"] == "My Project"

    row = conn.execute(
        "SELECT custom_title FROM sessions WHERE session_id = 'unrelated-3333'"
    ).fetchone()
    assert row["custom_title"] is None

    conn.close()


def test_resolve_session_id_after_rename(tmp_path):
    """After /rename, process args contain the old title but the DB has the new one.

    The resolver should follow the slug to find the newest session, not the one
    whose title still matches the stale process args.
    """
    conn = _make_db(tmp_path)

    # Older continuation: still has the old title
    upsert_session(conn, {
        "session_id": "old-1111",
        "slug": "my-slug",
        "custom_title": "Old Name",
        "modified_at": "2026-01-01T00:00:00Z",
    })

    # Current session: renamed, newest modified_at
    upsert_session(conn, {
        "session_id": "new-2222",
        "slug": "my-slug",
        "custom_title": "New Name",
        "modified_at": "2026-01-02T00:00:00Z",
    })
    conn.commit()

    # Process was started with the old title before the rename happened
    proc = {"pid": 1234, "tty": "ttys000", "resume_arg": "Old Name"}
    result = _resolve_session_id(conn, proc)
    assert result == "new-2222"

    conn.close()


def test_scan_runtime_matches_pidless_row_after_clear(tmp_path):
    """After /clear, the hook creates a runtime row for the new session (pid=NULL).

    The process's --resume arg still references the old session name.
    scan_runtime should match the process to the new session by CWD, not
    resolve it to the stale old session via the --resume arg.
    """
    conn = _make_db(tmp_path)

    # Old session: had a title, now ended (no runtime row)
    upsert_session(conn, {
        "session_id": "old-session",
        "slug": "old-slug",
        "custom_title": "My Project",
        "cwd": "/projects/myapp",
        "modified_at": "2026-01-01T00:00:00Z",
    })

    # New session after /clear: hook created session + runtime (pid=NULL)
    upsert_session(conn, {
        "session_id": "new-session",
        "cwd": "/projects/myapp",
        "modified_at": "2026-01-02T00:00:00Z",
    })
    upsert_runtime_state(conn, "new-session", "idle")
    conn.commit()

    # Verify pid is NULL
    row = conn.execute("SELECT pid FROM runtime WHERE session_id = 'new-session'").fetchone()
    assert row["pid"] is None

    # Mock: one process with --resume pointing to the OLD session name
    mock_processes = [{"pid": 5555, "tty": "ttys001", "resume_arg": "My Project"}]

    with patch("claude_status.scanner.get_claude_processes", return_value=mock_processes), \
         patch("claude_status.scanner.get_tmux_pane_map", return_value={}), \
         patch("claude_status.scanner.get_tmux_client_map", return_value={}), \
         patch("claude_status.scanner.get_process_cwd", return_value="/projects/myapp"):
        active_ids = scan_runtime(conn, detect_states=False)

    conn.commit()

    # The new session should be in active IDs and have the PID populated
    assert "new-session" in active_ids
    row = conn.execute("SELECT pid FROM runtime WHERE session_id = 'new-session'").fetchone()
    assert row["pid"] == 5555

    conn.close()


def test_inherit_title_after_clear(tmp_path):
    """After /clear, the new session should inherit the title from the previous session
    when resume_arg points to the old session's UUID."""
    conn = _make_db(tmp_path)

    # Old session with a title
    upsert_session(conn, {
        "session_id": "a7449d4e-64b9-47d9-be55-d6fdd174f7f3",
        "custom_title": "CoS Daily",
        "slug": "tidy-crafting-origami",
        "modified_at": "2026-01-01T00:00:00Z",
    })

    # New session after /clear: no title, no slug, but has cwd from the hook event
    upsert_session(conn, {
        "session_id": "new-session-after-clear",
        "cwd": "/projects/cos",
        "modified_at": "2026-01-02T00:00:00Z",
    })
    upsert_runtime_state(conn, "new-session-after-clear", "idle")
    conn.commit()

    # Process with --resume pointing to the OLD session UUID
    mock_processes = [{
        "pid": 8888,
        "tty": "ttys001",
        "resume_arg": "a7449d4e-64b9-47d9-be55-d6fdd174f7f3",
    }]

    with patch("claude_status.scanner.get_claude_processes", return_value=mock_processes), \
         patch("claude_status.scanner.get_tmux_pane_map", return_value={}), \
         patch("claude_status.scanner.get_tmux_client_map", return_value={}), \
         patch("claude_status.scanner.get_process_cwd", return_value="/projects/cos"):
        scan_runtime(conn, detect_states=False)

    conn.commit()
    row = conn.execute(
        "SELECT custom_title FROM sessions WHERE session_id = 'new-session-after-clear'"
    ).fetchone()
    assert row["custom_title"] == "CoS Daily"
    conn.close()


def test_scan_runtime_preserves_waiting_state(tmp_path):
    """Poll-based scan (detect_states=True) should not overwrite hook-set 'waiting'."""
    conn = _make_db(tmp_path)

    upsert_session(conn, {
        "session_id": "wait-sess",
        "jsonl_path": "/nonexistent/file.jsonl",
        "modified_at": "2026-01-01T00:00:00Z",
    })
    upsert_runtime_state(conn, "wait-sess", "waiting")
    conn.commit()

    mock_processes = [{"pid": 7777, "tty": "ttys001", "resume_arg": None}]

    with patch("claude_status.scanner.get_claude_processes", return_value=mock_processes), \
         patch("claude_status.scanner.get_tmux_pane_map", return_value={}), \
         patch("claude_status.scanner.get_tmux_client_map", return_value={}), \
         patch("claude_status.scanner.get_process_cwd", return_value=None), \
         patch("claude_status.scanner._resolve_session_id", return_value="wait-sess"), \
         patch("claude_status.scanner.detect_state", return_value=("idle", None)):
        scan_runtime(conn, detect_states=True)

    conn.commit()
    row = conn.execute("SELECT state FROM runtime WHERE session_id = 'wait-sess'").fetchone()
    assert row["state"] == "waiting"
    conn.close()


def test_scan_runtime_pid_map_prevents_stale_resolution(tmp_path):
    """If a runtime row already maps a PID to a session, scan_runtime should
    trust that mapping over _resolve_session_id (which uses stale --resume args).
    """
    conn = _make_db(tmp_path)

    upsert_session(conn, {
        "session_id": "correct-session",
        "cwd": "/projects/myapp",
        "modified_at": "2026-01-02T00:00:00Z",
    })
    # Runtime row already has the PID (e.g., from a previous scan)
    conn.execute(
        """INSERT INTO runtime (session_id, pid, state, updated_at)
           VALUES ('correct-session', 5555, 'working', '2026-01-02T00:00:00Z')"""
    )

    # Old session that _resolve_session_id would incorrectly match
    upsert_session(conn, {
        "session_id": "stale-session",
        "slug": "old-slug",
        "custom_title": "Old Title",
        "modified_at": "2026-01-01T00:00:00Z",
    })
    conn.commit()

    mock_processes = [{"pid": 5555, "tty": "ttys001", "resume_arg": "Old Title"}]

    with patch("claude_status.scanner.get_claude_processes", return_value=mock_processes), \
         patch("claude_status.scanner.get_tmux_pane_map", return_value={}), \
         patch("claude_status.scanner.get_tmux_client_map", return_value={}):
        active_ids = scan_runtime(conn, detect_states=False)

    # Should use the pid_map, not _resolve_session_id
    assert "correct-session" in active_ids
    assert "stale-session" not in active_ids

    conn.close()
