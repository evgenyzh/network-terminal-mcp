"""Unit tests for the raw terminal transport layer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.terminal import SerialTerminal, SshTerminal, _BaseTerminal


class ScriptedTerminal(_BaseTerminal):
    """A _BaseTerminal whose reads come from a scripted chunk queue."""

    _retry_delay = 0.001
    _prompt_retries = 3
    _prompt_last_read = 0.001
    _prompt_read_timeout = 0.1

    def __init__(self, chunks: list[str]) -> None:
        super().__init__()
        self._chunks = list(chunks)
        self.writes: list[bytes] = []

    def _read_channel(self) -> str:
        if self._chunks:
            return self._chunks.pop(0)
        return ""

    def _write_bytes(self, data: bytes) -> None:
        self.writes.append(data)


def test_read_until_pattern_retains_terminator_and_buffers_overflow() -> None:
    terminal = ScriptedTerminal(["show version\nline1\n--- more ---rest"])
    output = terminal.read_until_pattern(r"--- more ---", read_timeout=5.0)
    assert output == "show version\nline1\n--- more ---"
    assert terminal._buffer == "rest"


def test_read_until_pattern_times_out_when_pattern_never_appears() -> None:
    terminal = ScriptedTerminal(["no prompt here", "still nothing"])
    with pytest.raises(TransportError, match="not detected"):
        terminal.read_until_pattern(r"#\s*$", read_timeout=0.05)


def test_read_channel_timing_stops_when_no_new_data() -> None:
    terminal = ScriptedTerminal(["chunk-one\n", "", "", ""])
    output = terminal.read_channel_timing(last_read=0.01, read_timeout=2.0)
    assert output == "chunk-one\n"


def test_find_prompt_returns_the_last_line() -> None:
    terminal = ScriptedTerminal(["\n\nsw1#"])
    prompt = terminal.find_prompt()
    assert prompt == "sw1#"


@pytest.mark.parametrize(
    "prompt",
    [
        "<SW>", "[SW]", "[SW-bgp]", "[~SW]", "[*SW]", "[~SW-bgp]",
        "[*SW-GigabitEthernet0/0/1]", "[SW-bgp-af-ipv4]", "<SW-1>",
        "switch(config-if)#", "operator@router>", "operator@router#",
    ],
)
def test_find_prompt_recognizes_configuration_views(prompt: str) -> None:
    terminal = ScriptedTerminal([f"Welcome to the device\r\n{prompt} \r\n"])

    assert terminal.find_prompt() == prompt
    assert terminal.writes == [b"\n"]


@pytest.mark.parametrize(
    "output",
    [
        "status [SW]", "[edit protocols bgp]", "Continue? [Y/N]", "[]",
        "[~]", "[SW]command", "prefix switch#", "progress 100%",
        "[SW\x03]", "[[SW]]", "[Y/N]", "[yes/no]",
    ],
)
def test_find_prompt_rejects_non_prompt_lines(output: str) -> None:
    terminal = ScriptedTerminal([output])

    with pytest.raises(TransportError, match="unable to find"):
        terminal.find_prompt()


def test_find_prompt_raises_when_no_prompt() -> None:
    terminal = ScriptedTerminal([""])
    with pytest.raises(TransportError, match="unable to find"):
        terminal.find_prompt()


def test_send_command_writes_and_strips_echo() -> None:
    terminal = ScriptedTerminal(["show version\nCisco IOS 15.2\nsw1#"])
    output = terminal.send_command(
        "show version",
        expect_string=r"#\s*$",
        read_timeout=5.0,
        strip_prompt=False,
        strip_command=True,
        cmd_verify=True,
    )
    assert terminal.writes == [b"show version\n"]
    assert "Cisco IOS 15.2\nsw1#" in output


def test_normalizes_crlf_to_lf() -> None:
    terminal = ScriptedTerminal([b"line1\r\nline2\r\nsw1#".decode()])
    output = terminal.read_until_pattern(r"#\s*$", read_timeout=5.0)
    assert "\r" not in output
    assert output == "line1\nline2\nsw1#"


def test_strips_ansi_codes_from_reads() -> None:
    terminal = ScriptedTerminal(["\x1b[7m--More--\x1b[m"])
    output = terminal.read_until_pattern(r"--More--\s*\Z", read_timeout=5.0)
    assert output == "--More--"


def test_ssh_terminal_disconnect_without_connect_is_safe() -> None:
    terminal = SshTerminal()
    terminal.disconnect()
    assert terminal._channel is None
    assert terminal._client is None


_SERIAL_KWARGS = {
    "baudrate": 9600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "conn_timeout": 1.0,
}


def test_serial_rejects_relative_paths(tmp_path: Path) -> None:
    terminal = SerialTerminal()
    with pytest.raises(TransportError, match="absolute /dev/"):
        terminal.connect(device="ttyUSB0", **_SERIAL_KWARGS)  # type: ignore[arg-type]
    with pytest.raises(TransportError, match="absolute /dev/"):
        terminal.connect(device=str(tmp_path / "ttyUSB0"), **_SERIAL_KWARGS)  # type: ignore[arg-type]


def test_serial_rejects_missing_devices() -> None:
    terminal = SerialTerminal()
    with pytest.raises(TransportError, match="not accessible"):
        terminal.connect(
            device="/dev/opencode-network-terminal-mcp-missing",
            **_SERIAL_KWARGS,  # type: ignore[arg-type]
        )


def test_serial_rejects_non_character_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stat as stat_module

    monkeypatch.setattr(
        "network_terminal_mcp.terminal.os.stat",
        lambda path: os.stat_result((stat_module.S_IFREG | 0o644, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    )
    terminal = SerialTerminal()
    with pytest.raises(TransportError, match="not a character device"):
        terminal.connect(device="/dev/not-a-char", **_SERIAL_KWARGS)  # type: ignore[arg-type]


def test_serial_open_failure_is_a_transport_error() -> None:
    terminal = SerialTerminal()
    with pytest.raises(TransportError, match="serial open failed"):
        terminal.connect(device="/dev/null", **_SERIAL_KWARGS)  # type: ignore[arg-type]


def test_serial_disconnect_without_connect_is_safe() -> None:
    terminal = SerialTerminal()
    terminal.disconnect()
    assert terminal._serial is None
