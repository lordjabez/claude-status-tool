"""Session catalog scanning, ID resolution, and runtime process info."""

import json
import os
import re
import sqlite3
from pathlib import Path

from claude_status.db import update_runtime_process_info, upsert_runtime, upsert_session
from claude_status.process import (
    detect_state,
    get_claude_processes,
    get_process_cwd,
    get_tmux_client_map,
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
    2. JSONL parsing for fields the index lacks (custom_title, slug, cwd)
    3. Propagate custom_title across sessions that share a slug
    """
    if not PROJECTS_DIR.is_dir():
        return

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        project_dir_name = project_dir.name

        # Index provides fast metadata (message count, timestamps, branch)
        index_file = project_dir / "sessions-index.json"
        if index_file.is_file():
            _scan_index_file(conn, index_file, project_dir_name)

        # JSONL provides fields the index lacks (custom_title, slug, cwd).
        # Runs for all sessions; mtime guard prevents redundant re-parsing.
        _scan_jsonl_files(conn, project_dir, project_dir_name)

    # When Claude Code continues a session (compaction), the new JSONL gets
    # the same slug but no custom-title entry. Propagate titles from siblings.
    _propagate_titles(conn)


def _scan_index_file(
    conn: sqlite3.Connection,
    index_file: Path,
    project_dir_name: str,
) -> None:
    """Parse a sessions-index.json and upsert sessions."""
    try:
        with open(index_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    entries = data.get("entries", [])
    original_path = data.get("originalPath")
    project_path = original_path or folder_label(project_dir_name)

    for entry in entries:
        session_id = entry.get("sessionId")
        if not session_id:
            continue

        jsonl_path = entry.get("fullPath")

        # Don't set jsonl_mtime here — the index doesn't have slug/custom_title/cwd,
        # so we need JSONL parsing to fill those in. Setting mtime from the index
        # would cause the mtime guard in _scan_jsonl_files to skip the JSONL file.
        upsert_session(conn, {
            "session_id": session_id,
            "first_prompt": _truncate(entry.get("firstPrompt"), 200),
            "message_count": entry.get("messageCount", 0),
            "is_sidechain": 1 if entry.get("isSidechain") else 0,
            "git_branch": entry.get("gitBranch"),
            "project_path": project_path,
            "project_dir": project_dir_name,
            "jsonl_path": jsonl_path,
            "created_at": entry.get("created"),
            "modified_at": entry.get("modified"),
        })


def _scan_jsonl_files(
    conn: sqlite3.Connection,
    project_dir: Path,
    project_dir_name: str,
) -> None:
    """Parse JSONL files for fields the index doesn't provide (slug, custom_title, cwd)."""
    project_path = folder_label(project_dir_name)

    for jsonl_file in project_dir.glob("*.jsonl"):
        session_id = jsonl_file.stem

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


def _propagate_titles(conn: sqlite3.Connection) -> None:
    """Copy custom_title to sessions that share a slug but lack a title.

    This handles session continuations (compaction) where Claude Code reuses
    the slug but doesn't copy the custom-title entry to the new JSONL.
    """
    conn.execute("""
        UPDATE sessions
        SET custom_title = (
            SELECT s2.custom_title
            FROM sessions s2
            WHERE s2.slug = sessions.slug
              AND s2.custom_title IS NOT NULL
            ORDER BY s2.modified_at DESC
            LIMIT 1
        )
        WHERE custom_title IS NULL
          AND slug IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM sessions s2
            WHERE s2.slug = sessions.slug
              AND s2.custom_title IS NOT NULL
          )
    """)


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


def scan_runtime(conn: sqlite3.Connection, detect_states: bool = True) -> set[str]:
    """Detect running claude processes and update runtime info.

    When detect_states=True (default, used by poll), infers state from JSONL and
    does a full upsert_runtime.  When False (used by hook dispatch), only updates
    pid/tty/tmux without touching hook-set state.

    Returns set of active session IDs.
    """
    processes = get_claude_processes()
    if not processes:
        return set()

    # Build a PID→session_id map and session_id set from existing runtime rows.
    # Hook events create runtime rows with the authoritative session_id before
    # scan_runtime runs.  After /clear or /compact, the process's --resume arg
    # still references the old session name, so _resolve_session_id would map the
    # PID to the wrong session.  Trusting the existing runtime mapping avoids this.
    pid_map: dict[int, str] = {}
    runtime_session_ids: set[str] = set()
    for row in conn.execute("SELECT session_id, pid FROM runtime"):
        runtime_session_ids.add(row["session_id"])
        if row["pid"] is not None:
            pid_map[row["pid"]] = row["session_id"]

    tmux_map = get_tmux_pane_map()
    client_map = get_tmux_client_map()
    active_session_ids: set[str] = set()
    matched_pids: set[int] = set()

    for proc in processes:
        session_id = pid_map.get(proc["pid"]) or _resolve_session_id(conn, proc)
        if session_id is None:
            continue

        # In hook mode (detect_states=False), update_runtime_process_info is UPDATE-only
        # and no-ops when the resolved session has no runtime row. Treat the process as
        # unmatched so the CWD fallback in _match_pidless_runtime can assign it correctly.
        if not detect_states and session_id not in runtime_session_ids:
            continue

        active_session_ids.add(session_id)
        matched_pids.add(proc["pid"])

        process_data = _build_process_data(
            proc, session_id, tmux_map, client_map,
        )

        if detect_states:
            row = conn.execute(
                "SELECT jsonl_path FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            jsonl_path = row["jsonl_path"] if row else None
            state, last_activity = detect_state(jsonl_path)
            # JSONL-based detection can't distinguish "idle at prompt" from
            # "waiting on elicitation/permission", so preserve hook-set waiting.
            if state == "idle":
                rt_row = conn.execute(
                    "SELECT state FROM runtime WHERE session_id = ?", (session_id,)
                ).fetchone()
                if rt_row and rt_row["state"] == "waiting":
                    state = "waiting"
            process_data["state"] = state
            process_data["last_activity"] = last_activity
            upsert_runtime(conn, process_data)
        else:
            update_runtime_process_info(conn, process_data)

    # Match runtime rows that have no PID yet (hook-created after /clear or /compact)
    # to unmatched processes by CWD.
    _match_pidless_runtime(conn, processes, matched_pids, active_session_ids,
                           tmux_map, client_map, detect_states)

    return active_session_ids


def _build_process_data(
    proc: dict,
    session_id: str,
    tmux_map: dict[str, dict[str, str]],
    client_map: dict[str, str],
) -> dict:
    """Build the process_data dict for a resolved process."""
    tty = proc["tty"]
    tty_device = resolve_tty_device(tty)
    tmux_info = tmux_map.get(tty_device, {})

    # If running in tmux, use the client terminal's TTY instead of the pane PTY
    if tmux_info:
        client_tty = client_map.get(tmux_info.get("session", ""))
        if client_tty:
            tty = client_tty.removeprefix("/dev/")

    return {
        "session_id": session_id,
        "pid": proc["pid"],
        "tty": tty,
        "tmux_target": tmux_info.get("target"),
        "tmux_session": tmux_info.get("session"),
        "resume_arg": proc["resume_arg"],
    }


def _match_pidless_runtime(
    conn: sqlite3.Connection,
    processes: list[dict],
    matched_pids: set[int],
    active_session_ids: set[str],
    tmux_map: dict[str, dict[str, str]],
    client_map: dict[str, str],
    detect_states: bool,
) -> None:
    """Match runtime rows with no PID to unmatched processes by CWD.

    After /clear or /compact, hooks create a runtime row for the new session before
    the process scan runs.  The process's --resume arg still references the old
    session, so normal resolution misses the new row.  We match by comparing the
    process's working directory to the session's cwd stored in the sessions table.
    """
    pidless_rows = conn.execute(
        """SELECT r.session_id, s.cwd, s.project_path
           FROM runtime r
           JOIN sessions s ON r.session_id = s.session_id
           WHERE r.pid IS NULL""",
    ).fetchall()
    if not pidless_rows:
        return

    unmatched = [p for p in processes if p["pid"] not in matched_pids]
    if not unmatched:
        return

    # Build CWD→process map for unmatched processes
    cwd_procs: dict[str, dict] = {}
    for proc in unmatched:
        cwd = get_process_cwd(proc["pid"])
        if cwd:
            cwd_procs[cwd] = proc

    for row in pidless_rows:
        session_cwd = row["cwd"] or row["project_path"]
        if not session_cwd:
            continue
        proc = cwd_procs.get(session_cwd)
        if proc is None:
            continue

        active_session_ids.add(row["session_id"])
        matched_pids.add(proc["pid"])

        process_data = _build_process_data(
            proc, row["session_id"], tmux_map, client_map,
        )
        if detect_states:
            # No JSONL path available for CWD-matched processes; default to idle,
            # but preserve hook-set "waiting" (same rationale as main loop above).
            state = "idle"
            rt_row = conn.execute(
                "SELECT state FROM runtime WHERE session_id = ?",
                (row["session_id"],),
            ).fetchone()
            if rt_row and rt_row["state"] == "waiting":
                state = "waiting"
            process_data["state"] = state
            process_data["last_activity"] = None
            upsert_runtime(conn, process_data)
        else:
            update_runtime_process_info(conn, process_data)


def _resolve_session_id(conn: sqlite3.Connection, proc: dict) -> str | None:
    """Map a running process to a session ID.

    Resolution order:
    1. UUID in --resume arg: direct match
    2. Search string in --resume arg: match against custom_title/slug
    3. Bare claude (no --resume): match via CWD to project dir
    """
    resume_arg = proc.get("resume_arg")

    if resume_arg:
        if _looks_like_uuid(resume_arg):
            return resume_arg

        # It's a search string (custom title or slug) — try exact then partial
        for pattern in (resume_arg, f"%{resume_arg}%"):
            op = "=" if pattern == resume_arg else "LIKE"
            row = conn.execute(
                f"""SELECT session_id, slug FROM sessions
                    WHERE custom_title {op} ? OR slug {op} ?
                    ORDER BY modified_at DESC LIMIT 1""",
                (pattern, pattern),
            ).fetchone()
            if row:
                # A rename changes custom_title but not slug. The process args
                # still contain the old title, so this match may point at an
                # older sibling. Prefer the newest session sharing the same slug.
                slug = row["slug"]
                if slug:
                    newest = conn.execute(
                        """SELECT session_id FROM sessions
                           WHERE slug = ?
                           ORDER BY modified_at DESC LIMIT 1""",
                        (slug,),
                    ).fetchone()
                    if newest:
                        return newest["session_id"]
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
