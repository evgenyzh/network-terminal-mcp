"""Tests for the PTY and serial terminal transports."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.terminal import PtyTerminal, SerialTerminal

_RESIDENT_SCRIPT = """\
import sys
print("banner", flush=True)
for line in sys.stdin:
    line = line.rstrip("\\r\\n")
    if line == "quit":
        break
    print("got:" + line, flush=True)
print("bye", flush=True)
"""


def _start_pty(script: str) -> PtyTerminal:
    terminal = PtyTerminal()
    terminal.start((sys.executable, "-u", "-c", script))
    return terminal


def test_pty_persistent_write_and_read() -> None:
    terminal = _start_pty(_RESIDENT_SCRIPT)
    try:
        startup = terminal.read_channel_timing(last_read=0.02, read_timeout=1.0)
        assert "banner" in startup
        assert terminal.is_alive() is True

        terminal.write_channel("first\n")
        first = terminal.read_channel_timing(last_read=0.02, read_timeout=1.0)
        assert first.strip() == "got:first"

        terminal.write_channel("second\n")
        second = terminal.read_channel_timing(last_read=0.02, read_timeout=1.0)
        assert second.strip() == "got:second"
    finally:
        terminal.disconnect()


def test_pty_reports_exit_code_and_keeps_final_output() -> None:
    terminal = _start_pty("import sys; print('fatal', flush=True); sys.exit(7)")
    try:
        output = terminal.read_channel_timing(last_read=0.02, read_timeout=1.0)
        assert "fatal" in output
        assert terminal.is_alive() is False
        assert terminal.exit_code == 7
    finally:
        terminal.disconnect()


def test_pty_disconnect_is_safe_without_start() -> None:
    terminal = PtyTerminal()
    terminal.disconnect()
    assert terminal.is_alive() is False
    assert terminal.exit_code is None


def test_pty_disconnect_stops_the_child_process() -> None:
    terminal = _start_pty("import time; print('ready', flush=True); time.sleep(60)")
    output = terminal.read_channel_timing(last_read=0.02, read_timeout=1.0)
    assert "ready" in output
    process = terminal._process
    assert process is not None
    pid = process.pid

    terminal.disconnect()

    assert terminal.is_alive() is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_serial_rejects_missing_devices() -> None:
    terminal = SerialTerminal()
    with pytest.raises(TransportError, match="not accessible"):
        terminal.open(device="/dev/opencode-network-terminal-mcp-missing", baudrate=9600)


def test_serial_rejects_non_character_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = tmp_path / "tty-test"
    device.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "network_terminal_mcp.terminal.os.stat",
        lambda _: os.stat_result(
            (stat.S_IFREG | 0o644, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        ),
    )
    with pytest.raises(TransportError, match="not a character device"):
        terminal = SerialTerminal()
        terminal.open(device=str(device), baudrate=9600)


class FakeSerial:
    def __init__(self, **_: Any) -> None:
        self.is_open = True
        self.writes: list[bytes] = []
        self.incoming: list[bytes] = []
        self.in_waiting = 0

    def read(self, size: int) -> bytes:
        if self.incoming:
            data = self.incoming.pop(0)
            self.in_waiting = sum(len(item) for item in self.incoming)
            return data
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def test_serial_read_write_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeSerial] = []

    def factory(**kwargs: Any) -> FakeSerial:
        port = FakeSerial(**kwargs)
        created.append(port)
        return port

    fake_module = SimpleNamespace(
        Serial=factory,
        EIGHTBITS=8,
        PARITY_NONE="N",
        STOPBITS_ONE=1,
    )
    monkeypatch.setitem(sys.modules, "serial", fake_module)  # type: ignore[arg-type]
    monkeypatch.setattr(SerialTerminal, "_validate_device", staticmethod(lambda _: None))

    terminal = SerialTerminal()
    terminal.open(device="/dev/ttyTEST", baudrate=115200)
    assert terminal.is_alive() is True

    terminal.write_channel("show version\r")
    assert created[0].writes == [b"show version\r"]

    created[0].incoming = [b"hello\r\n"]
    created[0].in_waiting = len(created[0].incoming[0])
    assert terminal.read_channel_timing(last_read=0.01, read_timeout=1.0) == "hello\r\n"

    terminal.disconnect()
    assert terminal.is_alive() is False
