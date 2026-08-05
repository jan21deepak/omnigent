"""Tests for OSC 8 hyperlinks in ``omni host status`` output."""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from omnigent import cli

_SERVER_URL = "https://omnigent-9775947-fe-vm-fe-randy-pitch-4.aws.databricksapps.com"
_LOG_PATH = "/Users/example/.omnigent/logs/host/host-20260801-074914-755244.log"


def _render(monkeypatch: pytest.MonkeyPatch, *, width: int, terminal: bool = True) -> str:
    """Render one daemon payload through a narrow terminal-like console."""
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=width,
        force_terminal=terminal or None,
        color_system="truecolor" if terminal else None,
        highlight=False,
    )
    monkeypatch.setattr(cli, "_host_console", lambda: console)
    cli._echo_daemon_payloads(
        [
            {
                "target": _SERVER_URL,
                "mode": "server",
                "pid": 74299,
                "process": "offline",
                "host_status": "unknown",
                "server_url": _SERVER_URL,
                "host_id": "host_77599b2c44934910b00cfdfda3ba21fc",
                "log_path": _LOG_PATH,
                "error": None,
            }
        ]
    )
    return stream.getvalue()


def test_server_url_is_an_explicit_hyperlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """The click target carries the full URL even when the text is truncated."""
    output = _render(monkeypatch, width=60)

    assert "\x1b]8;id=" in output
    assert f"{_SERVER_URL}\x1b\\" in output
    assert "…" in output


def test_log_path_is_a_file_hyperlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon log paths become ``file://`` click targets."""
    output = _render(monkeypatch, width=60)

    assert f"file://{_LOG_PATH}\x1b\\" in output


def test_status_lines_never_soft_wrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """No status line exceeds the terminal width, so nothing soft-wraps."""
    output = _render(monkeypatch, width=60)

    for line in output.splitlines():
        assert len(Text.from_ansi(line).plain) <= 60


def test_redirected_output_keeps_full_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped or redirected output is not shortened: there is no link to protect."""
    output = _render(monkeypatch, width=60, terminal=False)

    unwrapped = output.replace("\n", "")
    assert _SERVER_URL in unwrapped
    assert _LOG_PATH in unwrapped
    assert "…" not in output


def test_relative_values_are_not_linked() -> None:
    """Values that are not URLs or absolute paths get no click target."""
    assert cli._host_link_target("relative/path.log") is None
    assert cli._host_link_target(None) is None
    assert cli._host_link_target("host status failed (404): host not found") is None
