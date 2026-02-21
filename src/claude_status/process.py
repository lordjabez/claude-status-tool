"""Process detection, tmux mapping, and debug log checks."""

import re
import subprocess
import time
from pathlib import Path

DEBUG_DIR = Path.home() / ".claude" / "debug"

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
    """Extract the --resume argument value if present."""
    parts = args.split()
    try:
        idx = parts.index("--resume")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    except ValueError:
        pass
    return None


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


def get_debug_log_mtime(session_id: str) -> float | None:
    """Get the mtime of a session's debug log file."""
    debug_file = DEBUG_DIR / f"{session_id}.txt"
    try:
        return debug_file.stat().st_mtime
    except OSError:
        return None


def detect_state(session_id: str, activity_threshold: float = 5.0) -> str:
    """Detect whether a running session is active or idle.

    - Debug log mtime within threshold seconds: 'active'
    - Otherwise: 'idle'
    """
    mtime = get_debug_log_mtime(session_id)
    if mtime is not None:
        age = time.time() - mtime
        if age <= activity_threshold:
            return "active"
    return "idle"


def resolve_tty_device(tty: str) -> str:
    """Convert ps TTY format to /dev/ path for tmux matching.

    ps shows TTYs like 'ttys001' but tmux uses '/dev/ttys001'.
    """
    if tty.startswith("/dev/"):
        return tty
    if tty == "??" or tty == "?":
        return tty
    return f"/dev/{tty}"
