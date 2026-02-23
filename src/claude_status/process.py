"""Process detection, tmux mapping, and state detection."""

import json
import re
import subprocess
import time
from pathlib import Path

# Patterns to exclude from claude process detection
_EXCLUDE_PATTERNS = [
    "tmux",
    "/Applications/Claude",
    "Claude.app",
    "claude-status",
]


def get_claude_processes() -> list[dict]:
    """Parse ps output for running claude processes.

    Returns list of {"pid": int, "tty": str, "resume_arg": str | None}.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,args"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []

    processes = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue

        parts = line.split(None, 2)
        if len(parts) < 3:
            continue

        pid_str, tty, args = parts
        if not _is_claude_process(args):
            continue

        try:
            pid = int(pid_str)
        except ValueError:
            continue

        resume_arg = _extract_resume_arg(args)
        processes.append({
            "pid": pid,
            "tty": tty,
            "resume_arg": resume_arg,
        })

    return processes


def _is_claude_process(args: str) -> bool:
    """Check if a process args string represents a Claude CLI session."""
    for pattern in _EXCLUDE_PATTERNS:
        if pattern in args:
            return False
    # Match 'claude' as a standalone command (possibly with path)
    # but not claude-something (like claude-status)
    return bool(re.search(r"(?:^|/)claude(?:\s|$)", args))


def _extract_resume_arg(args: str) -> str | None:
    """Extract the --resume argument value if present.

    The resume value can be multi-word (e.g. 'claude --resume Claude Status Tool'),
    so we capture everything after '--resume '.
    """
    marker = " --resume "
    idx = args.find(marker)
    if idx == -1:
        return None
    value = args[idx + len(marker):].strip()
    return value if value else None


def get_process_cwd(pid: int) -> str | None:
    """Get the current working directory of a process via lsof."""
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    lines = result.stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "fcwd" and i + 1 < len(lines):
            name_line = lines[i + 1]
            if name_line.startswith("n"):
                return name_line[1:]

    return None


def get_tmux_pane_map() -> dict[str, dict[str, str]]:
    """Map TTY devices to tmux pane info.

    Returns {"/dev/ttysNNN": {"target": "session:win.pane", "session": "session_name"}}.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_tty} #{session_name}:#{window_index}.#{pane_index} #{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}

    if result.returncode != 0:
        return {}

    pane_map: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 3:
            tty, target, session_name = parts
            pane_map[tty] = {"target": target, "session": session_name}
        elif len(parts) == 2:
            tty, target = parts
            pane_map[tty] = {"target": target, "session": ""}

    return pane_map


def get_tmux_client_map() -> dict[str, str]:
    """Map tmux session names to the TTY of the attached client terminal.

    Returns {"session_name": "/dev/ttysNNN"}.
    If multiple clients are attached to one session, the last one wins.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-clients", "-F", "#{client_tty} #{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}

    if result.returncode != 0:
        return {}

    client_map: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            client_map[parts[1]] = parts[0]

    return client_map


def detect_state(
    jsonl_path: str | None, activity_threshold: float = 10.0,
) -> tuple[str, float | None]:
    """Detect session state from the JSONL conversation file.

    Returns (state, jsonl_mtime) where state is one of:
    - 'working': JSONL modified within threshold (Claude is processing)
    - 'waiting': last entry is a tool use request (needs user permission/response)
    - 'idle': Claude is at the prompt waiting for user input
    """
    if jsonl_path is None:
        return "idle", None

    path = Path(jsonl_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "idle", None

    if time.time() - mtime <= activity_threshold:
        return "working", mtime

    last_entry = _read_last_jsonl_entry(path)
    if last_entry and last_entry.get("type") == "assistant":
        content = last_entry.get("message", {}).get("content", [])
        has_tool_use = any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in content
        )
        if has_tool_use:
            return "waiting", mtime

    return "idle", mtime


def _read_last_jsonl_entry(path: Path) -> dict | None:
    """Read and parse the last entry from a JSONL file.

    Reads from the tail of the file to avoid loading the entire file.
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return None
    except OSError:
        return None

    read_size = min(size, 256 * 1024)
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - read_size))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(data.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    return None


def resolve_tty_device(tty: str) -> str:
    """Convert ps TTY format to /dev/ path for tmux matching.

    ps shows TTYs like 'ttys001' but tmux uses '/dev/ttys001'.
    """
    if tty.startswith("/dev/"):
        return tty
    if tty == "??" or tty == "?":
        return tty
    return f"/dev/{tty}"
