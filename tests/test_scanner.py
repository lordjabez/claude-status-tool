"""Tests for claude_status.scanner module."""

import json
from pathlib import Path

from claude_status.db import get_connection, init_schema, upsert_session
from claude_status.scanner import (
    _looks_like_uuid,
    _parse_jsonl,
    _propagate_titles,
    _resolve_session_id,
    _truncate,
    folder_label,
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
