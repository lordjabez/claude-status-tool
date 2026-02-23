"""Generate a mock claude-status.db with realistic session/runtime permutations.

Usage:
    uv run python tests/generate_mock_db.py [output_path]

Defaults to ./mock-claude-status.db in the current directory.
"""

import sqlite3
import sys
import time
from pathlib import Path

# Add src to path so we can reuse the real schema
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_status.db import (  # noqa: E402
    get_connection,
    init_schema,
    update_meta,
    upsert_runtime,
    upsert_session,
)

CLAUDE_PROJECTS = "/Users/dev/.claude/projects"


def _jsonl_path(project_dir: str, session_id: str) -> str:
    return f"{CLAUDE_PROJECTS}/{project_dir}/{session_id}.jsonl"


def generate(db_path: Path) -> None:
    conn = get_connection(db_path)
    init_schema(conn)

    now = time.time()
    DAY = 86400

    # -----------------------------------------------------------------
    # Sessions: cover a range of metadata combinations
    # -----------------------------------------------------------------

    sessions = [
        # Active: all fields populated
        {
            "session_id": "a1b2c3d4-1111-4000-8000-000000000001",
            "slug": "bold-running-falcon",
            "custom_title": "API Refactor",
            "project_path": "/Users/dev/Projects/acme/backend",
            "project_dir": "-Users-dev-Projects-acme-backend",
            "cwd": "/Users/dev/Projects/acme/backend",
            "git_branch": "feat/api-v2",
            "first_prompt": "Refactor the REST API to use FastAPI",
            "message_count": 87,
            "is_sidechain": 0,
            "jsonl_mtime": now - 5,
            "created_at": "2026-02-18T09:00:00Z",
            "modified_at": "2026-02-20T14:30:00Z",
        },
        # Active: slug only, no custom title
        {
            "session_id": "a1b2c3d4-2222-4000-8000-000000000002",
            "slug": "quiet-thinking-otter",
            "project_path": "/Users/dev/Projects/acme/backend",
            "project_dir": "-Users-dev-Projects-acme-backend",
            "cwd": "/Users/dev/Projects/acme/backend",
            "git_branch": "main",
            "first_prompt": "Write tests for the auth middleware",
            "message_count": 23,
            "jsonl_mtime": now - 2,
            "created_at": "2026-02-20T10:00:00Z",
            "modified_at": "2026-02-20T14:28:00Z",
        },
        # Idle: different project, custom title
        {
            "session_id": "a1b2c3d4-3333-4000-8000-000000000003",
            "slug": "swift-coding-eagle",
            "custom_title": "Frontend Dashboard",
            "project_path": "/Users/dev/Projects/acme/dashboard",
            "project_dir": "-Users-dev-Projects-acme-dashboard",
            "cwd": "/Users/dev/Projects/acme/dashboard",
            "git_branch": "feat/charts",
            "first_prompt": "Add a real-time chart component using D3",
            "message_count": 156,
            "jsonl_mtime": now - 300,
            "created_at": "2026-02-19T08:00:00Z",
            "modified_at": "2026-02-20T14:00:00Z",
        },
        # Idle: no branch, no title
        {
            "session_id": "a1b2c3d4-4444-4000-8000-000000000004",
            "slug": "gentle-morning-sparrow",
            "project_path": "/Users/dev/Projects/personal/blog",
            "project_dir": "-Users-dev-Projects-personal-blog",
            "cwd": "/Users/dev/Projects/personal/blog",
            "first_prompt": "Help me write about async Python patterns",
            "message_count": 12,
            "jsonl_mtime": now - 600,
            "created_at": "2026-02-20T11:00:00Z",
            "modified_at": "2026-02-20T11:45:00Z",
        },
        # Active: sidechain (subagent)
        {
            "session_id": "a1b2c3d4-5555-4000-8000-000000000005",
            "slug": "hidden-working-bee",
            "project_path": "/Users/dev/Projects/acme/backend",
            "project_dir": "-Users-dev-Projects-acme-backend",
            "cwd": "/Users/dev/Projects/acme/backend",
            "git_branch": "feat/api-v2",
            "first_prompt": "Research pagination strategies for GraphQL",
            "message_count": 8,
            "is_sidechain": 1,
            "jsonl_mtime": now - 10,
            "created_at": "2026-02-20T14:25:00Z",
            "modified_at": "2026-02-20T14:29:00Z",
        },
        # Inactive: old, high message count
        {
            "session_id": "a1b2c3d4-6666-4000-8000-000000000006",
            "slug": "ancient-sailing-whale",
            "custom_title": "Database Migration",
            "project_path": "/Users/dev/Projects/acme/backend",
            "project_dir": "-Users-dev-Projects-acme-backend",
            "cwd": "/Users/dev/Projects/acme/backend",
            "git_branch": "feat/db-migration",
            "first_prompt": "Migrate from MySQL to PostgreSQL",
            "message_count": 342,
            "jsonl_mtime": now - DAY * 3,
            "created_at": "2026-02-10T09:00:00Z",
            "modified_at": "2026-02-17T18:00:00Z",
        },
        # Inactive: no slug or title (minimal, JSONL fallback)
        {
            "session_id": "a1b2c3d4-7777-4000-8000-000000000007",
            "project_path": "/Users/dev",
            "project_dir": "-Users-dev",
            "cwd": "/Users/dev",
            "first_prompt": "What's the weather like?",
            "message_count": 2,
            "jsonl_mtime": now - DAY * 7,
            "created_at": "2026-02-13T15:00:00Z",
            "modified_at": "2026-02-13T15:02:00Z",
        },
        # Inactive: zero messages (created but abandoned)
        {
            "session_id": "a1b2c3d4-8888-4000-8000-000000000008",
            "slug": "empty-starting-cloud",
            "project_path": "/Users/dev/Projects/scratch",
            "project_dir": "-Users-dev-Projects-scratch",
            "cwd": "/Users/dev/Projects/scratch",
            "git_branch": "main",
            "message_count": 0,
            "jsonl_mtime": now - DAY * 2,
            "created_at": "2026-02-18T16:00:00Z",
            "modified_at": "2026-02-18T16:00:00Z",
        },
        # Inactive: long first prompt (truncated in real DB)
        {
            "session_id": "a1b2c3d4-9999-4000-8000-000000000009",
            "slug": "verbose-planning-fox",
            "custom_title": "Infrastructure Overhaul",
            "project_path": "/Users/dev/Projects/acme/infra",
            "project_dir": "-Users-dev-Projects-acme-infra",
            "cwd": "/Users/dev/Projects/acme/infra",
            "git_branch": "main",
            "first_prompt": (
                "Implement the following plan:\n\n"
                "# Infrastructure Overhaul\n\n## Context\n\n"
                "Our current infrastructure uses a mix of manually "
                "provisioned EC2 instances and some ECS services."
            ),
            "message_count": 201,
            "jsonl_mtime": now - DAY,
            "created_at": "2026-02-15T09:00:00Z",
            "modified_at": "2026-02-19T17:30:00Z",
        },
        # Active: home directory project (no subfolder)
        {
            "session_id": "a1b2c3d4-aaaa-4000-8000-000000000010",
            "slug": "curious-wandering-cat",
            "custom_title": "Quick Question",
            "project_path": "/Users/dev",
            "project_dir": "-Users-dev",
            "cwd": "/Users/dev",
            "first_prompt": "How do I configure SSH agent forwarding?",
            "message_count": 4,
            "jsonl_mtime": now - 30,
            "created_at": "2026-02-20T14:20:00Z",
            "modified_at": "2026-02-20T14:25:00Z",
        },
        # Inactive: same project, different branch
        {
            "session_id": "a1b2c3d4-bbbb-4000-8000-000000000011",
            "slug": "steady-building-crane",
            "custom_title": "Hotfix Auth Bug",
            "project_path": "/Users/dev/Projects/acme/backend",
            "project_dir": "-Users-dev-Projects-acme-backend",
            "cwd": "/Users/dev/Projects/acme/backend",
            "git_branch": "hotfix/auth-bypass",
            "first_prompt": "Critical auth bypass in JWT validation",
            "message_count": 31,
            "jsonl_mtime": now - DAY * 5,
            "created_at": "2026-02-12T22:00:00Z",
            "modified_at": "2026-02-13T01:30:00Z",
        },
        # Inactive: hyphenated project name
        {
            "session_id": "a1b2c3d4-cccc-4000-8000-000000000012",
            "slug": "bright-crafting-robin",
            "project_path": "/Users/dev/Projects/open-source/my-cool-tool",
            "project_dir": "-Users-dev-Projects-open-source-my-cool-tool",
            "cwd": "/Users/dev/Projects/open-source/my-cool-tool",
            "git_branch": "main",
            "first_prompt": "Add CI/CD pipeline with GitHub Actions",
            "message_count": 45,
            "jsonl_mtime": now - DAY * 2,
            "created_at": "2026-02-16T10:00:00Z",
            "modified_at": "2026-02-18T14:00:00Z",
        },
    ]

    for s in sessions:
        s.setdefault("is_sidechain", 0)
        s["jsonl_path"] = _jsonl_path(s["project_dir"], s["session_id"])
        upsert_session(conn, s)

    # -----------------------------------------------------------------
    # Runtime: active/idle with various tmux/tty combinations
    # -----------------------------------------------------------------

    runtime_entries = [
        # Active, tmux, resumed by UUID
        {
            "session_id": "a1b2c3d4-1111-4000-8000-000000000001",
            "pid": 12001, "tty": "ttys003",
            "tmux_target": "work:0.0", "tmux_session": "work",
            "resume_arg": "a1b2c3d4-1111-4000-8000-000000000001",
            "state": "working", "last_activity": now - 1,
        },
        # Active, tmux, resumed by name
        {
            "session_id": "a1b2c3d4-2222-4000-8000-000000000002",
            "pid": 12002, "tty": "ttys004",
            "tmux_target": "work:1.0", "tmux_session": "work",
            "resume_arg": "quiet-thinking-otter",
            "state": "working", "last_activity": now - 2,
        },
        # Idle, tmux, stale debug log
        {
            "session_id": "a1b2c3d4-3333-4000-8000-000000000003",
            "pid": 12003, "tty": "ttys005",
            "tmux_target": "work:2.0", "tmux_session": "work",
            "resume_arg": "Frontend Dashboard",
            "state": "idle", "last_activity": now - 300,
        },
        # Idle, different tmux session
        {
            "session_id": "a1b2c3d4-4444-4000-8000-000000000004",
            "pid": 12004, "tty": "ttys010",
            "tmux_target": "personal:0.0", "tmux_session": "personal",
            "resume_arg": "gentle-morning-sparrow",
            "state": "idle", "last_activity": now - 600,
        },
        # Active sidechain, same pane as parent
        {
            "session_id": "a1b2c3d4-5555-4000-8000-000000000005",
            "pid": 12005, "tty": "ttys003",
            "tmux_target": "work:0.0", "tmux_session": "work",
            "resume_arg": "a1b2c3d4-5555-4000-8000-000000000005",
            "state": "working", "last_activity": now - 3,
        },
        # Active, no tmux (bare terminal), no resume arg
        {
            "session_id": "a1b2c3d4-aaaa-4000-8000-000000000010",
            "pid": 12010, "tty": "ttys020",
            "state": "working", "last_activity": now - 1,
        },
    ]

    for r in runtime_entries:
        upsert_runtime(conn, r)

    # -----------------------------------------------------------------
    # Meta
    # -----------------------------------------------------------------

    update_meta(conn, "last_poll", "2026-02-20T14:30:00-0800")

    conn.commit()
    conn.close()

    # Print summary
    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM runtime WHERE state = 'working'"
    ).fetchone()[0]
    idle = conn.execute(
        "SELECT COUNT(*) FROM runtime WHERE state = 'idle'"
    ).fetchone()[0]
    inactive = total - active - idle
    projects = conn.execute(
        "SELECT COUNT(DISTINCT project_path) FROM sessions"
    ).fetchone()[0]
    sidechains = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE is_sidechain = 1"
    ).fetchone()[0]
    conn.close()

    print(f"Generated {db_path}")
    print(f"  {total} sessions across {projects} projects")
    print(f"  {active} working, {idle} idle, {inactive} inactive")
    print(f"  {sidechains} sidechain(s)")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("mock-claude-status.db")
    if out.exists():
        out.unlink()
    generate(out)
