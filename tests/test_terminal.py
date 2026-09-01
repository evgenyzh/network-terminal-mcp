"""Unit tests for the raw terminal transport layer."""

from __future__ import annotations

import pytest

from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.terminal import SshTerminal, _BaseTerminal


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
