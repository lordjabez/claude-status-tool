"""SQLite database schema, connection management, and query helpers."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".claude" / "claude-status.db"


def get_db_path() -> Path:
    """Return the database path, respecting CLAUDE_STATUS_DB env var."""
    env = os.environ.get("CLAUDE_STATUS_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and row factory."""
    if path is None:
        path = get_db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id     TEXT PRIMARY KEY,
            slug           TEXT,
            custom_title   TEXT,
            project_path   TEXT,
            project_dir    TEXT,
            cwd            TEXT,
            git_branch     TEXT,
            first_prompt   TEXT,
            message_count  INTEGER DEFAULT 0 CHECK(message_count >= 0),
            is_sidechain   INTEGER DEFAULT 0,
            jsonl_path     TEXT,
            jsonl_mtime    REAL,
            created_at     TEXT,
            modified_at    TEXT,
            updated_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime (
            session_id     TEXT PRIMARY KEY REFERENCES sessions(session_id),
            pid            INTEGER,
            tty            TEXT,
            tmux_target    TEXT,
            tmux_session   TEXT,
            resume_arg     TEXT,
            state          TEXT NOT NULL CHECK(state IN ('working', 'idle', 'waiting')),
            last_activity    REAL,
            updated_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_project_path ON sessions(project_path);
        CREATE INDEX IF NOT EXISTS idx_sessions_modified_at ON sessions(modified_at);
        CREATE INDEX IF NOT EXISTS idx_runtime_state ON runtime(state);
        CREATE INDEX IF NOT EXISTS idx_sessions_slug ON sessions(slug);
    """)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_session(conn: sqlite3.Connection, data: dict) -> None:
    """Upsert a session row, preserving existing non-NULL values.

    Uses COALESCE so that a NULL in the new data won't overwrite
    an existing value (e.g. custom_title set by JSONL but absent from the index).
    """
    data.setdefault("updated_at", _now())
    columns = [
        "session_id", "slug", "custom_title", "project_path", "project_dir",
        "cwd", "git_branch", "first_prompt", "message_count", "is_sidechain",
        "jsonl_path", "jsonl_mtime", "created_at", "modified_at", "updated_at",
    ]
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    values = [data.get(c) for c in columns]

    # On conflict, prefer the new value if non-NULL, else keep the old one.
    # updated_at always takes the new value.
    updates = ", ".join(
        f"{c} = COALESCE(excluded.{c}, sessions.{c})" if c != "updated_at"
        else f"{c} = excluded.{c}"
        for c in columns if c != "session_id"
    )
    conn.execute(
        f"INSERT INTO sessions ({col_str}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id) DO UPDATE SET {updates}",
        values,
    )


def upsert_runtime(conn: sqlite3.Connection, data: dict) -> None:
    """INSERT OR REPLACE a runtime row."""
    data.setdefault("updated_at", _now())
    columns = [
        "session_id", "pid", "tty", "tmux_target", "tmux_session",
        "resume_arg", "state", "last_activity", "updated_at",
    ]
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    values = [data.get(c) for c in columns]
    conn.execute(
        f"INSERT OR REPLACE INTO runtime ({col_str}) VALUES ({placeholders})",
        values,
    )


def remove_stale_runtime(conn: sqlite3.Connection, active_session_ids: set[str]) -> None:
    """Delete runtime rows for sessions no longer running."""
    if not active_session_ids:
        conn.execute("DELETE FROM runtime")
        return
    placeholders = ", ".join(["?"] * len(active_session_ids))
    conn.execute(
        f"DELETE FROM runtime WHERE session_id NOT IN ({placeholders})",
        list(active_session_ids),
    )


def update_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a meta key-value pair."""
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a meta value by key."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# Common SELECT used by all session query helpers.
_SESSION_SELECT = """
    SELECT s.*, r.pid, r.tty, r.tmux_target, r.tmux_session,
           r.resume_arg, r.state, r.last_activity
    FROM sessions s
"""


def get_active_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return sessions that have a runtime entry."""
    return conn.execute(
        _SESSION_SELECT
        + "JOIN runtime r ON s.session_id = r.session_id "
        + "ORDER BY r.state ASC, s.modified_at DESC"
    ).fetchall()


def get_all_sessions(
    conn: sqlite3.Connection,
    project_filter: str | None = None,
    state_filter: str | None = None,
) -> list[sqlite3.Row]:
    """Return all sessions, optionally filtered, with runtime info if available."""
    query = _SESSION_SELECT + "LEFT JOIN runtime r ON s.session_id = r.session_id "
    conditions = []
    params: list[str] = []
    if project_filter:
        conditions.append("(s.project_path LIKE ? OR s.project_dir LIKE ?)")
        params.extend([f"%{project_filter}%", f"%{project_filter}%"])
    if state_filter:
        if state_filter == "inactive":
            conditions.append("r.state IS NULL")
        else:
            conditions.append("r.state = ?")
            params.append(state_filter)
    if conditions:
        query += "WHERE " + " AND ".join(conditions) + " "
    query += "ORDER BY COALESCE(r.state, 'zzz') ASC, s.modified_at DESC"
    return conn.execute(query, params).fetchall()


def get_session(conn: sqlite3.Connection, partial_id: str) -> sqlite3.Row | None:
    """Get a single session by exact or partial ID match."""
    base = _SESSION_SELECT + "LEFT JOIN runtime r ON s.session_id = r.session_id "
    row = conn.execute(base + "WHERE s.session_id = ?", (partial_id,)).fetchone()
    if row:
        return row
    return conn.execute(
        base + "WHERE s.session_id LIKE ? ORDER BY s.modified_at DESC LIMIT 1",
        (f"{partial_id}%",),
    ).fetchone()
