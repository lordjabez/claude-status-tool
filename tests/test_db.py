"""Tests for claude_status.db module."""

import sqlite3
from pathlib import Path

import pytest

from claude_status.db import (
    delete_runtime,
    get_active_sessions,
    get_all_sessions,
    get_connection,
    get_meta,
    get_session,
    init_schema,
    remove_stale_runtime,
    update_meta,
    update_runtime_process_info,
    upsert_runtime,
    upsert_runtime_state,
    upsert_session,
)


def _make_db(tmp_path: Path):
    """Create a temp DB for testing."""
    conn = get_connection(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_init_schema_creates_tables(tmp_path):
    conn = _make_db(tmp_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in tables}
    assert "sessions" in names
    assert "runtime" in names
    assert "meta" in names
    conn.close()


def test_init_schema_idempotent(tmp_path):
    conn = _make_db(tmp_path)
    init_schema(conn)  # second call should not raise
    conn.close()


def test_upsert_session(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {
        "session_id": "abc-123",
        "slug": "test-session",
        "project_path": "/test/path",
        "message_count": 5,
    })
    conn.commit()

    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'abc-123'").fetchone()
    assert row is not None
    assert row["slug"] == "test-session"
    assert row["message_count"] == 5

    # Upsert with updated data
    upsert_session(conn, {
        "session_id": "abc-123",
        "slug": "updated-slug",
        "project_path": "/test/path",
        "message_count": 10,
    })
    conn.commit()

    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'abc-123'").fetchone()
    assert row["slug"] == "updated-slug"
    assert row["message_count"] == 10
    conn.close()


def test_upsert_session_null_preserves_existing(tmp_path):
    """NULL in new data should not overwrite an existing non-NULL value."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {
        "session_id": "abc-123",
        "slug": "my-slug",
        "custom_title": "My Title",
        "cwd": "/some/path",
        "message_count": 5,
    })
    conn.commit()

    # Second upsert omits slug, custom_title, cwd (they'll be None)
    upsert_session(conn, {
        "session_id": "abc-123",
        "message_count": 10,
        "project_path": "/new/project",
    })
    conn.commit()

    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'abc-123'").fetchone()
    assert row["slug"] == "my-slug"
    assert row["custom_title"] == "My Title"
    assert row["cwd"] == "/some/path"
    assert row["message_count"] == 10
    assert row["project_path"] == "/new/project"
    conn.close()


def test_upsert_runtime(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "abc-123"})
    upsert_runtime(conn, {
        "session_id": "abc-123",
        "pid": 12345,
        "state": "working",
        "tty": "ttys001",
    })
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 'abc-123'").fetchone()
    assert row["pid"] == 12345
    assert row["state"] == "working"
    conn.close()


def test_remove_stale_runtime(tmp_path):
    conn = _make_db(tmp_path)
    for sid in ["s1", "s2", "s3"]:
        upsert_session(conn, {"session_id": sid})
        upsert_runtime(conn, {"session_id": sid, "state": "idle"})
    conn.commit()

    remove_stale_runtime(conn, {"s1", "s3"})
    conn.commit()

    rows = conn.execute("SELECT session_id FROM runtime ORDER BY session_id").fetchall()
    ids = [r["session_id"] for r in rows]
    assert ids == ["s1", "s3"]
    conn.close()


def test_remove_stale_runtime_empty_set(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    upsert_runtime(conn, {"session_id": "s1", "state": "idle"})
    conn.commit()

    remove_stale_runtime(conn, set())
    conn.commit()

    rows = conn.execute("SELECT * FROM runtime").fetchall()
    assert len(rows) == 0
    conn.close()


def test_meta(tmp_path):
    conn = _make_db(tmp_path)
    update_meta(conn, "last_poll", "2026-01-01T00:00:00")
    conn.commit()

    assert get_meta(conn, "last_poll") == "2026-01-01T00:00:00"
    assert get_meta(conn, "nonexistent") is None

    update_meta(conn, "last_poll", "2026-01-02T00:00:00")
    conn.commit()
    assert get_meta(conn, "last_poll") == "2026-01-02T00:00:00"
    conn.close()


def test_get_active_sessions(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1", "modified_at": "2026-01-01T00:00:00"})
    upsert_session(conn, {"session_id": "s2", "modified_at": "2026-01-02T00:00:00"})
    upsert_runtime(conn, {"session_id": "s1", "state": "working"})
    conn.commit()

    rows = get_active_sessions(conn)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    conn.close()


def test_get_all_sessions_with_filters(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {
        "session_id": "s1",
        "project_path": "/Users/jud/Projects/foo",
        "modified_at": "2026-01-01",
    })
    upsert_session(conn, {
        "session_id": "s2",
        "project_path": "/Users/jud/Projects/bar",
        "modified_at": "2026-01-02",
    })
    upsert_runtime(conn, {"session_id": "s1", "state": "working"})
    conn.commit()

    # All sessions
    rows = get_all_sessions(conn)
    assert len(rows) == 2

    # Project filter
    rows = get_all_sessions(conn, project_filter="foo")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"

    # State filter
    rows = get_all_sessions(conn, state_filter="working")
    assert len(rows) == 1

    rows = get_all_sessions(conn, state_filter="inactive")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s2"
    conn.close()


def test_get_session_partial_id(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {
        "session_id": "abc12345-full-uuid-here",
        "modified_at": "2026-01-01",
    })
    conn.commit()

    # Exact match
    row = get_session(conn, "abc12345-full-uuid-here")
    assert row is not None

    # Partial match
    row = get_session(conn, "abc123")
    assert row is not None
    assert row["session_id"] == "abc12345-full-uuid-here"

    # No match
    row = get_session(conn, "zzz")
    assert row is None
    conn.close()


def test_foreign_key_enforcement(tmp_path):
    """Runtime insert with a nonexistent session_id should fail."""
    conn = _make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        upsert_runtime(conn, {
            "session_id": "nonexistent-session",
            "state": "idle",
        })
    conn.close()


def test_invalid_state_rejected(tmp_path):
    """Runtime state must be one of working, idle, waiting."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        upsert_runtime(conn, {
            "session_id": "s1",
            "state": "bogus",
        })
    conn.close()


def test_negative_message_count_rejected(tmp_path):
    """message_count must be >= 0."""
    conn = _make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (session_id, message_count, updated_at) VALUES (?, ?, ?)",
            ("s1", -1, "2026-01-01T00:00:00"),
        )
    conn.close()


def test_upsert_runtime_state_insert(tmp_path):
    """upsert_runtime_state creates a new runtime row if none exists."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    conn.commit()

    upsert_runtime_state(conn, "s1", "working", 1234567890.0)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 's1'").fetchone()
    assert row["state"] == "working"
    assert row["last_activity"] == 1234567890.0
    conn.close()


def test_upsert_runtime_state_preserves_fields(tmp_path):
    """upsert_runtime_state should not wipe pid/tty/tmux already set by process scan."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    upsert_runtime(conn, {
        "session_id": "s1",
        "pid": 42,
        "tty": "ttys001",
        "tmux_target": "main:0.0",
        "state": "idle",
        "last_activity": 100.0,
    })
    conn.commit()

    upsert_runtime_state(conn, "s1", "working", 200.0)
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 's1'").fetchone()
    assert row["state"] == "working"
    assert row["last_activity"] == 200.0
    assert row["pid"] == 42
    assert row["tty"] == "ttys001"
    assert row["tmux_target"] == "main:0.0"
    conn.close()


def test_upsert_runtime_state_null_activity_preserves_existing(tmp_path):
    """Passing last_activity=None should keep the existing value via COALESCE."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    upsert_runtime(conn, {
        "session_id": "s1",
        "state": "working",
        "last_activity": 500.0,
    })
    conn.commit()

    upsert_runtime_state(conn, "s1", "idle")
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 's1'").fetchone()
    assert row["state"] == "idle"
    assert row["last_activity"] == 500.0
    conn.close()


def test_upsert_runtime_state_fk_violation(tmp_path):
    """upsert_runtime_state should raise IntegrityError for nonexistent session."""
    conn = _make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        upsert_runtime_state(conn, "nonexistent", "working")
    conn.close()


def test_update_runtime_process_info_preserves_state(tmp_path):
    """update_runtime_process_info should set pid/tty/tmux without touching state."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    upsert_runtime(conn, {
        "session_id": "s1",
        "pid": 100,
        "tty": "ttys000",
        "state": "waiting",
        "last_activity": 500.0,
    })
    conn.commit()

    update_runtime_process_info(conn, {
        "session_id": "s1",
        "pid": 200,
        "tty": "ttys001",
        "tmux_target": "main:0.0",
        "tmux_session": "main",
        "resume_arg": "my-session",
    })
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 's1'").fetchone()
    assert row["pid"] == 200
    assert row["tty"] == "ttys001"
    assert row["tmux_target"] == "main:0.0"
    assert row["tmux_session"] == "main"
    assert row["resume_arg"] == "my-session"
    # State and last_activity must be untouched
    assert row["state"] == "waiting"
    assert row["last_activity"] == 500.0
    conn.close()


def test_update_runtime_process_info_noop_on_missing_row(tmp_path):
    """update_runtime_process_info should be a no-op if the runtime row doesn't exist."""
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    conn.commit()

    # No runtime row exists; this should not raise or create one.
    update_runtime_process_info(conn, {
        "session_id": "s1",
        "pid": 200,
        "tty": "ttys001",
    })
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 's1'").fetchone()
    assert row is None
    conn.close()


def test_delete_runtime(tmp_path):
    conn = _make_db(tmp_path)
    upsert_session(conn, {"session_id": "s1"})
    upsert_runtime(conn, {"session_id": "s1", "state": "idle"})
    conn.commit()

    delete_runtime(conn, "s1")
    conn.commit()

    row = conn.execute("SELECT * FROM runtime WHERE session_id = 's1'").fetchone()
    assert row is None
    conn.close()


def test_delete_runtime_nonexistent(tmp_path):
    """Deleting a runtime row that doesn't exist should not raise."""
    conn = _make_db(tmp_path)
    delete_runtime(conn, "nonexistent")  # Should not raise
    conn.commit()
    conn.close()
