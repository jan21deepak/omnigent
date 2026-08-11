"""Canonical native harness tool names shared by built-in write policies."""

from __future__ import annotations

from typing import Any

CLAUDE_NATIVE_EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)

# Argument keys Claude Code uses for file-targeting native write/edit tools.
_CLAUDE_NATIVE_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "notebook_path")


def claude_native_write_path(args: dict[str, Any]) -> str:
    """Return the target path from Claude native write/edit tool arguments.

    Omnigent ``sys_os_*`` tools use ``path``; Claude Code native tools use
    ``file_path`` (``Write`` / ``Edit`` / ``MultiEdit``) or ``notebook_path``
    (``NotebookEdit``).
    """
    for key in _CLAUDE_NATIVE_PATH_KEYS:
        value = args.get(key)
        # Skip empty strings so a blank ``path`` falls through to
        # ``file_path`` / ``notebook_path`` (same as ``or``-chain fallback).
        if isinstance(value, str) and value:
            return value
    return ""
