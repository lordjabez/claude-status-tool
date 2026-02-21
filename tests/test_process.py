"""Tests for claude_status.process module."""

from claude_status.process import (
    _extract_resume_arg,
    _is_claude_process,
    resolve_tty_device,
)


def test_is_claude_process_basic():
    assert _is_claude_process("claude --resume abc-123")
    assert _is_claude_process("claude")
    assert _is_claude_process("/usr/local/bin/claude --resume test")


def test_is_claude_process_excludes():
    assert not _is_claude_process("tmux new -s claude")
    assert not _is_claude_process("/Applications/Claude.app/Contents/MacOS/Claude")
    assert not _is_claude_process("claude-status daemon start")


def test_extract_resume_arg():
    assert _extract_resume_arg("claude --resume abc-123") == "abc-123"
    assert _extract_resume_arg("claude") is None
    assert _extract_resume_arg("/usr/bin/claude --resume test") == "test"
    # Multi-word resume values (custom titles with spaces)
    assert _extract_resume_arg("claude --resume Claude Status Tool") == "Claude Status Tool"
    assert _extract_resume_arg("claude --resume My Project") == "My Project"


def test_resolve_tty_device():
    assert resolve_tty_device("ttys001") == "/dev/ttys001"
    assert resolve_tty_device("/dev/ttys001") == "/dev/ttys001"
    assert resolve_tty_device("??") == "??"
