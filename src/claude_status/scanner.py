"""Session catalog scanning, ID resolution, and state detection."""

import json
import os
import re
import sqlite3
from pathlib import Path

from claude_status.db import upsert_runtime, upsert_session
from claude_status.process import (
    detect_state,
    get_claude_processes,
    get_debug_log_mtime,
    get_process_cwd,
    get_tmux_pane_map,
    resolve_tty_device,
)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def folder_label(project_dir_name: str) -> str:
    """Convert project dir name back to a readable filesystem path.

    Dir names use hyphens as path separators, e.g.
    '-Users-jud-Projects-ips-chief-of-staff' -> '/Users/jud/Projects/ips/chief-of-staff'

    We reconstruct by checking which segments exist on disk.
    """
    if not project_dir_name.startswith("-"):
        return project_dir_name

    raw = project_dir_name.lstrip("-")
    parts = raw.split("-")
    segments: list[str] = []
    i = 0
    while i < len(parts):
        best = None
        for j in range(len(parts), i, -1):
            candidate = "-".join(parts[i:j])
            test_path = "/" + "/".join(segments + [candidate]) if segments else "/" + candidate
            if os.path.exists(test_path):
                best = candidate
                i = j
                break
        if best is None:
            best = parts[i]
            i += 1
        segments.append(best)

    return "/" + "/".join(segments)


def scan_sessions(conn: sqlite3.Connection) -> None:
    """Scan all session sources and upsert into the database.

    1. Walk sessions-index.json files for fast metadata
    2. Fall back to JSONL parsing for sessions not in any index
    """
    if not PROJECTS_DIR.is_dir():
        return

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        project_dir_name = project_dir.name
        indexed_ids: set[str] = set()

        # Try sessions-index.json first
        index_file = project_dir / "sessions-index.json"
        if index_file.is_file():
            indexed_ids = _scan_index_file(conn, index_file, project_dir_name)

        # Fall back to JSONL for sessions not in index
        _scan_jsonl_files(conn, project_dir, project_dir_name, indexed_ids)


def _scan_index_file(
    conn: sqlite3.Connection,
    index_file: Path,
    project_dir_name: str,
) -> set[str]:
    """Parse a sessions-index.json and upsert sessions. Returns set of session IDs found."""
    indexed_ids: set[str] = set()
    try:
        with open(index_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return indexed_ids

    entries = data.get("entries", [])
    original_path = data.get("originalPath")
    project_path = original_path or folder_label(project_dir_name)

    for entry in entries:
        session_id = entry.get("sessionId")
        if not session_id:
            continue

        indexed_ids.add(session_id)
        jsonl_path = entry.get("fullPath")
        jsonl_mtime = entry.get("fileMtime")
        if jsonl_mtime:
            # Convert ms to seconds
            jsonl_mtime = jsonl_mtime / 1000.0

        upsert_session(conn, {
            "session_id": session_id,
            "first_prompt": _truncate(entry.get("firstPrompt"), 200),
            "message_count": entry.get("messageCount", 0),
            "is_sidechain": 1 if entry.get("isSidechain") else 0,
            "git_branch": entry.get("gitBranch"),
            "project_path": project_path,
            "project_dir": project_dir_name,
            "jsonl_path": jsonl_path,
            "jsonl_mtime": jsonl_mtime,
            "created_at": entry.get("created"),
            "modified_at": entry.get("modified"),
        })

    return indexed_ids


def _scan_jsonl_files(
    conn: sqlite3.Connection,
    project_dir: Path,
    project_dir_name: str,
    skip_ids: set[str],
) -> None:
    """Parse JSONL files not covered by the index."""
    project_path = folder_label(project_dir_name)

    for jsonl_file in project_dir.glob("*.jsonl"):
        session_id = jsonl_file.stem
        if session_id in skip_ids:
            continue

        # Check mtime to avoid re-parsing unchanged files
        try:
            current_mtime = jsonl_file.stat().st_mtime
        except OSError:
            continue

        # Check if we already have this session with the same mtime
        row = conn.execute(
            "SELECT jsonl_mtime FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        stored_mtime = row["jsonl_mtime"] if row else None
        if stored_mtime is not None and abs(stored_mtime - current_mtime) < 0.01:
            continue

        session_data = _parse_jsonl(jsonl_file)
        if session_data is None:
            continue

        upsert_session(conn, {
            "session_id": session_id,
            "slug": session_data.get("slug"),
            "custom_title": session_data.get("title"),
            "cwd": session_data.get("cwd"),
            "first_prompt": _truncate(session_data.get("first_user_text"), 200),
            "message_count": session_data.get("message_count", 0),
            "project_path": project_path,
            "project_dir": project_dir_name,
            "jsonl_path": str(jsonl_file),
            "jsonl_mtime": current_mtime,
            "created_at": session_data.get("first_ts"),
            "modified_at": session_data.get("last_ts"),
        })


def _parse_jsonl(filepath: Path) -> dict | None:
    """Extract metadata from a session JSONL file.

    Ported from claude-sessions parse_session().
    """
    title = None
    slug = None
    cwd = None
    first_ts = None
    last_ts = None
    message_count = 0
    first_user_text = None

    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")

                if entry_type == "custom-title":
                    raw = entry.get("customTitle", "")
                    title = raw.split("\n")[0].strip()

                if entry_type == "user":
                    ts = entry.get("timestamp")
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    if slug is None:
                        slug = entry.get("slug")
                    if cwd is None:
                        cwd = entry.get("cwd")
                    if first_user_text is None:
                        msg = entry.get("message", {})
                        parts = []
                        for block in msg.get("content", []):
                            if isinstance(block, str):
                                parts.append(block)
                            elif isinstance(block, dict) and block.get("type") == "text":
                                parts.append(block.get("text", ""))
                        text = "".join(parts).strip()
                        if text and not text.startswith("[Request interrupted"):
                            first_user_text = text

                if entry_type == "assistant":
                    ts = entry.get("timestamp")
                    if ts:
                        last_ts = ts
                    message_count += 1
    except OSError:
        return None

    if first_ts is None:
        return None

    return {
        "title": title,
        "slug": slug,
        "cwd": cwd,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "message_count": message_count,
        "first_user_text": first_user_text,
    }


def scan_runtime(conn: sqlite3.Connection) -> set[str]:
    """Detect running claude processes and build runtime state.

    Returns set of active session IDs.
    """
    processes = get_claude_processes()
    if not processes:
        return set()

    tmux_map = get_tmux_pane_map()
    active_session_ids: set[str] = set()

    for proc in processes:
        session_id = _resolve_session_id(conn, proc)
        if session_id is None:
            continue

        active_session_ids.add(session_id)

        tty = proc["tty"]
        tty_device = resolve_tty_device(tty)
        tmux_info = tmux_map.get(tty_device, {})

        state = detect_state(session_id)
        debug_mtime = get_debug_log_mtime(session_id)

        upsert_runtime(conn, {
            "session_id": session_id,
            "pid": proc["pid"],
            "tty": tty,
            "tmux_target": tmux_info.get("target"),
            "tmux_session": tmux_info.get("session"),
            "resume_arg": proc["resume_arg"],
            "state": state,
            "debug_mtime": debug_mtime,
        })

    return active_session_ids


def _resolve_session_id(conn: sqlite3.Connection, proc: dict) -> str | None:
    """Map a running process to a session ID.

    Resolution order:
    1. UUID in --resume arg: direct match
    2. Search string in --resume arg: match against custom_title/slug
    3. Bare claude (no --resume): match via CWD to project dir
    """
    resume_arg = proc.get("resume_arg")

    if resume_arg:
        # Check if it's a UUID (direct session ID)
        if _looks_like_uuid(resume_arg):
            # Verify it exists in our sessions table
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?",
                (resume_arg,),
            ).fetchone()
            if row:
                return resume_arg
            # Even if not in DB yet, trust the resume arg
            return resume_arg

        # It's a search string (custom title or slug)
        row = conn.execute(
            """SELECT session_id FROM sessions
               WHERE custom_title = ? OR slug = ?
               ORDER BY modified_at DESC LIMIT 1""",
            (resume_arg, resume_arg),
        ).fetchone()
        if row:
            return row["session_id"]

        # Partial match
        row = conn.execute(
            """SELECT session_id FROM sessions
               WHERE custom_title LIKE ? OR slug LIKE ?
               ORDER BY modified_at DESC LIMIT 1""",
            (f"%{resume_arg}%", f"%{resume_arg}%"),
        ).fetchone()
        if row:
            return row["session_id"]

        return None

    # Bare claude process: resolve via CWD
    cwd = get_process_cwd(proc["pid"])
    if cwd is None:
        return None

    # Match CWD to a project path and find most recent session
    row = conn.execute(
        """SELECT session_id FROM sessions
           WHERE project_path = ? OR cwd = ?
           ORDER BY modified_at DESC LIMIT 1""",
        (cwd, cwd),
    ).fetchone()
    if row:
        return row["session_id"]

    return None


def _looks_like_uuid(s: str) -> bool:
    """Check if a string looks like a UUID."""
    return bool(_UUID_RE.match(s))


def _truncate(text: str | None, max_len: int) -> str | None:
    """Truncate text to max length."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "\u2026"
