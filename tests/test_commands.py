"""Tests for command-only connection parsing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from network_terminal_mcp import commands
from network_terminal_mcp.commands import ParsedCommand, parse_command
from network_terminal_mcp.errors import CommandError


@pytest.fixture(autouse=True)
def fake_client_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        commands.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("ssh", "telnet") else None,
    )


def test_ssh_keeps_all_options_and_arguments() -> None:
    parsed = parse_command(
        "ssh -J operator@bastion.example.net -p 2222 operator@192.0.2.10"
    )
    assert parsed == ParsedCommand(
        kind="ssh",
        command="ssh -J operator@bastion.example.net -p 2222 operator@192.0.2.10",
        argv=(
            "/usr/bin/ssh",
            "-J",
            "operator@bastion.example.net",
            "-p",
            "2222",
            "operator@192.0.2.10",
        ),
    )


def test_telnet_keeps_host_and_port() -> None:
    parsed = parse_command("telnet 192.0.2.30 2001")
    assert parsed.kind == "telnet"
    assert parsed.argv == ("/usr/bin/telnet", "192.0.2.30", "2001")


def test_shell_operators_are_plain_arguments_not_executed(tmp_path: Path) -> None:
    marker = str(tmp_path / "should-not-exist")
    parsed = parse_command(f"ssh operator@192.0.2.10 ; touch {marker}")
    assert ";" in parsed.argv
    assert not os.path.exists(marker)


def test_quoted_arguments_are_preserved() -> None:
    parsed = parse_command("ssh -o ProxyCommand='nc %h %p' operator@192.0.2.10")
    assert "-o" in parsed.argv
    assert "ProxyCommand=nc %h %p" in parsed.argv


def test_serial_defaults_to_9600() -> None:
    parsed = parse_command("serial /dev/ttyUSB0")
    assert parsed.kind == "serial"
    assert parsed.device == "/dev/ttyUSB0"
    assert parsed.baudrate == 9600
    assert parsed.argv == ()


def test_serial_accepts_baudrate_and_expands_user() -> None:
    parsed = parse_command("serial ~/tty-test 115200")
    assert parsed.device == os.path.expanduser("~/tty-test")
    assert parsed.baudrate == 115200


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("console 192.0.2.1", "unsupported command"),
        ("ssh", "requires connection arguments"),
        ("telnet", "requires connection arguments"),
        ("serial", "requires a device"),
        ("serial /dev/ttyUSB0 9600 extra", "only DEVICE"),
        ("serial /dev/ttyUSB0 fast", "integer"),
        ("serial /dev/ttyUSB0 0", "between 1"),
    ],
)
def test_invalid_commands_are_rejected(command: str, message: str) -> None:
    with pytest.raises(CommandError, match=message):
        parse_command(command)


def test_unbalanced_quotes_are_rejected() -> None:
    with pytest.raises(CommandError, match="cannot parse"):
        parse_command("ssh 'operator@192.0.2.10")


def test_missing_client_has_a_specific_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands.shutil, "which", lambda _: None)
    with pytest.raises(CommandError) as error:
        parse_command("telnet 192.0.2.30")
    assert error.value.code == "executable_not_found"
    assert str(error.value) == (
        "executable_not_found: telnet executable was not found on PATH"
    )
