"""Demo mode: populate the database with mock sessions cycling through states."""

import random
import signal
import time
import uuid

from claude_status.daemon import _notify_udp
from claude_status.db import (
    delete_runtime,
    get_connection,
    init_schema,
    upsert_runtime,
    upsert_session,
)

_MOCK_SESSIONS = [
    {
        "custom_title": "API Gateway",
        "slug": "warm-spinning-quasar",
        "project_path": "/projects/api-gateway",
        "cwd": "/projects/api-gateway",
        "git_branch": "feature/rate-limiting",
        "first_prompt": "Add rate limiting middleware to the API gateway",
    },
    {
        "custom_title": "Data Pipeline",
        "slug": "bold-dancing-neutron",
        "project_path": "/projects/data-pipeline",
        "cwd": "/projects/data-pipeline",
        "git_branch": "main",
        "first_prompt": "Fix the Parquet serialization bug in the ETL step",
    },
    {
        "custom_title": "Frontend Redesign",
        "slug": "quiet-painting-fern",
        "project_path": "/projects/frontend",
        "cwd": "/projects/frontend",
        "git_branch": "redesign-v2",
        "first_prompt": "Migrate the dashboard components to the new design system",
    },
    {
        "custom_title": "Infra Automation",
        "slug": "crisp-gliding-falcon",
        "project_path": "/projects/infra",
        "cwd": "/projects/infra",
        "git_branch": "terraform-modules",
        "first_prompt": "Refactor the VPC module to support multi-region",
    },
    {
        "custom_title": "ML Training",
        "slug": "deep-humming-river",
        "project_path": "/projects/ml-training",
        "cwd": "/projects/ml-training",
        "git_branch": "experiment/transformer-v3",
        "first_prompt": "Tune hyperparameters for the new transformer architecture",
    },
]

_STATES = ["idle", "working", "waiting"]

# Weighted transitions: from_state -> [(to_state, weight), ...]
# Working is the most common state; waiting is rare.
_TRANSITIONS = {
    "idle": [("working", 5), ("idle", 1)],
    "working": [("working", 4), ("idle", 2), ("waiting", 1)],
    "waiting": [("working", 3), ("idle", 1), ("waiting", 1)],
}

_shutdown = False


def _weighted_choice(transitions: list[tuple[str, int]]) -> str:
    states, weights = zip(*transitions)
    return random.choices(states, weights=weights, k=1)[0]


def _cleanup_demo_rows() -> None:
    """Delete all demo-* rows from sessions and runtime tables."""
    conn = get_connection()
    conn.execute("DELETE FROM runtime WHERE session_id LIKE 'demo-%'")
    conn.execute("DELETE FROM sessions WHERE session_id LIKE 'demo-%'")
    conn.commit()
    conn.close()
    _notify_udp()


def run_demo(count: int, interval: float) -> None:
    """Run the demo loop, creating mock sessions and cycling their states.

    Args:
        count: Number of mock sessions to create (capped at len(_MOCK_SESSIONS)).
        interval: Seconds between state transitions.
    """
    global _shutdown
    _shutdown = False

    count = min(count, len(_MOCK_SESSIONS))
    templates = random.sample(_MOCK_SESSIONS, count)

    conn = get_connection()
    init_schema(conn)

    # Clean up any previous demo sessions
    _cleanup_demo_rows()

    sessions: list[dict] = []
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fake_pid = 90000

    for i, tmpl in enumerate(templates):
        session_id = f"demo-{uuid.uuid4()}"
        state = random.choice(_STATES)
        pid = fake_pid + i

        upsert_session(conn, {
            "session_id": session_id,
            "slug": tmpl["slug"],
            "custom_title": tmpl["custom_title"],
            "project_path": tmpl["project_path"],
            "cwd": tmpl["cwd"],
            "git_branch": tmpl["git_branch"],
            "first_prompt": tmpl["first_prompt"],
            "message_count": random.randint(5, 200),
            "created_at": now_iso,
            "modified_at": now_iso,
        })
        upsert_runtime(conn, {
            "session_id": session_id,
            "pid": pid,
            "tty": f"ttys{900 + i:03d}",
            "state": state,
            "last_activity": time.time(),
        })
        sessions.append({"index": i, "session_id": session_id, "state": state, **tmpl})

    conn.commit()
    _notify_udp()

    print(f"Demo: {count} mock session(s), updating every {interval}s. Ctrl+C to stop.")
    for s in sessions:
        print(f"  [{s['state']:<8}] {s['custom_title']}")

    def _handle_signal(signum, frame):
        global _shutdown
        _shutdown = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not _shutdown:
            time.sleep(interval)
            if _shutdown:
                break

            # Pick a random session and transition its state
            s = random.choice(sessions)
            old_state = s["state"]
            new_state = _weighted_choice(_TRANSITIONS[old_state])

            if new_state == old_state:
                continue

            s["state"] = new_state
            now = time.time()

            if new_state == "idle" and random.random() < 0.15:
                # Occasionally "end" a session and start a fresh one
                delete_runtime(conn, s["session_id"])
                conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (s["session_id"],)
                )
                new_id = f"demo-{uuid.uuid4()}"
                new_state = "working"
                s["session_id"] = new_id
                s["state"] = new_state
                upsert_session(conn, {
                    "session_id": new_id,
                    "slug": s["slug"],
                    "custom_title": s["custom_title"],
                    "project_path": s["project_path"],
                    "cwd": s["cwd"],
                    "git_branch": s["git_branch"],
                    "first_prompt": s["first_prompt"],
                    "message_count": random.randint(1, 50),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                upsert_runtime(conn, {
                    "session_id": new_id,
                    "pid": fake_pid + s["index"],
                    "tty": f"ttys{900 + s['index']:03d}",
                    "state": new_state,
                    "last_activity": now,
                })
                conn.commit()
                _notify_udp()
                print(f"  {s['custom_title']}: restarted -> {new_state}")
                continue

            upsert_runtime(conn, {
                "session_id": s["session_id"],
                "pid": fake_pid + s["index"],
                "tty": f"ttys{900 + s['index']:03d}",
                "state": new_state,
                "last_activity": now if new_state == "working" else None,
            })
            conn.commit()
            _notify_udp()
            print(f"  {s['custom_title']}: {old_state} -> {new_state}")
    finally:
        conn.close()
        print("\nDemo: cleaning up...")
        _cleanup_demo_rows()
