"""Unit tests for command-open raw terminal sessions."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from network_terminal_mcp import commands
from network_terminal_mcp.commands import ParsedCommand
from network_terminal_mcp.errors import (
    CommandError,
    CredentialError,
    SessionError,
    TransportError,
)
from network_terminal_mcp.sessions import SessionInfo, SessionManager, SessionState
from network_terminal_mcp.sessions.manager import RuntimeLimits
from network_terminal_mcp.terminal import TerminalConnection


class FakeTerminal:
    def __init__(
        self,
        *,
        outputs: list[str] | None = None,
        alive: bool = True,
        exit_code_value: int | None = None,
    ) -> None:
        self.outputs = list(outputs or [])
        self.alive = alive
        self.exit_code_value = exit_code_value
        self.writes: list[str] = []
        self.read_timeouts: list[float] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.disconnected = False

    def write_channel(self, out_data: str) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(out_data)

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        self.read_timeouts.append(read_timeout)
        if self.read_error is not None:
            raise self.read_error
        return self.outputs.pop(0) if self.outputs else ""

    def is_alive(self) -> bool:
        return self.alive and not self.disconnected

    @property
    def exit_code(self) -> int | None:
        return self.exit_code_value

    def disconnect(self) -> None:
        self.disconnected = True
        self.alive = False
        if self.exit_code_value is None:
            self.exit_code_value = 0


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


class Harness:
    def __init__(
        self,
        terminals: list[FakeTerminal] | None = None,
        *,
        limits: RuntimeLimits | None = None,
        audit: FakeAudit | None = None,
    ) -> None:
        self.terminals = list(terminals or [FakeTerminal()])
        self.parsed: list[ParsedCommand] = []
        self.audit = audit or FakeAudit()
        self.manager = SessionManager(
            limits=limits or _limits(),
            terminal_factory=self._factory,
            audit_logger=self.audit,
        )

    def _factory(self, parsed: ParsedCommand) -> TerminalConnection:
        self.parsed.append(parsed)
        if not self.terminals:
            raise AssertionError("no fake terminal left")
        return self.terminals.pop(0)

    def open(self, command: str = "ssh operator@192.0.2.1") -> SessionInfo:
        return self.manager.open_session(command)


def test_show_controls_does_not_expose_known_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETWORK_TERMINAL_SHOW_CONTROLS", "1")
    secret = "secret\x1b[31mword"
    terminal = FakeTerminal(outputs=["prompt", f"{secret} \x1b[6n"])
    harness = Harness([terminal])
    info = harness.open()
    harness.manager.terminal_read(info.session_id)
    harness.manager.terminal_write_secret(info.session_id, value=secret)
    result = harness.manager.terminal_read(info.session_id).output
    assert result == "<redacted> \\x1b[6n"
    assert secret not in json.dumps(harness.audit.records)


def _limits(**overrides: object) -> RuntimeLimits:
    values: dict[str, object] = {
        "io_timeout": 1.0,
        "max_read_timeout": 10.0,
        "max_write_bytes": 1024,
        "max_inline_output_bytes": 1024,
        "max_session_buffer_bytes": 4096,
        "max_open_sessions": 10,
        "read_quiet_seconds": 0.001,
        "startup_timeout": 0.01,
        "startup_quiet_seconds": 0.001,
        "startup_output_tail_bytes": 256,
    }
    values.update(overrides)
    return RuntimeLimits(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def fake_client_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        commands.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("ssh", "telnet") else None,
    )


def _open(harness: Harness, command: str = "ssh operator@192.0.2.1") -> SessionInfo:
    return harness.open(command)


# ---------------------------------------------------------------------------
# open_session
# ---------------------------------------------------------------------------


def test_open_uses_the_parsed_command_and_returns_metadata() -> None:
    harness = Harness([FakeTerminal(outputs=["banner\n"])])
    info = _open(harness, "ssh -J operator@bastion.example.net operator@192.0.2.1")

    assert info.state is SessionState.READY
    assert info.command == "ssh -J operator@bastion.example.net operator@192.0.2.1"
    assert info.warnings == []
    assert info.exit_code is None
    assert harness.parsed[0].kind == "ssh"
    assert harness.parsed[0].argv[0] == "/usr/bin/ssh"
    assert harness.audit.records[0]["event"] == "open_session"
    assert harness.audit.records[0]["outcome"] == "started"
    assert harness.audit.records[0]["protocol"] == "ssh"
    assert harness.audit.records[-1]["outcome"] == "completed"
    assert harness.manager.read_output(info.session_id).output == "banner\n"


def test_open_tracks_startup_output_time_and_idle_seconds() -> None:
    harness = Harness([FakeTerminal(outputs=["banner\n"])])
    info = _open(harness)

    assert info.last_output_at is not None
    assert info.last_output_at >= info.created_at
    assert info.idle_seconds >= 0


def test_session_without_output_is_idle_since_creation() -> None:
    harness = Harness()
    info = _open(harness)
    assert info.last_output_at is None
    time.sleep(0.02)

    status = harness.manager.session_status(info.session_id)

    assert status.last_output_at is None
    assert status.idle_seconds > info.idle_seconds


def test_empty_read_does_not_refresh_last_output() -> None:
    harness = Harness()
    info = _open(harness)

    assert harness.manager.terminal_read(info.session_id, timeout=0.01).output == ""
    status = harness.manager.session_status(info.session_id)

    assert status.last_output_at is None


def test_new_output_refreshes_last_output() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("hello\n")

    assert harness.manager.terminal_read(info.session_id, timeout=0.01).output == "hello\n"
    status = harness.manager.session_status(info.session_id)

    assert status.last_output_at is not None
    assert status.last_output_at >= info.created_at
    assert status.idle_seconds < 1.0


def test_first_terminal_read_returns_the_startup_output() -> None:
    harness = Harness([FakeTerminal(outputs=["banner\nlogin: "])])
    info = _open(harness)

    first = harness.manager.terminal_read(info.session_id, timeout=5.0)

    assert first.output == "banner\nlogin: "
    assert first.output_offset == 0
    assert harness.manager.terminal_read(info.session_id, timeout=5.0).output == ""


def test_open_warns_when_there_is_no_initial_output() -> None:
    harness = Harness()
    info = _open(harness)
    assert info.state is SessionState.READY
    assert any("no initial output" in warning for warning in info.warnings)


def test_open_fails_with_real_exit_code_and_output() -> None:
    terminal = FakeTerminal(
        outputs=["Host key verification failed.\n"],
        alive=False,
        exit_code_value=255,
    )
    harness = Harness([terminal])

    with pytest.raises(TransportError, match="exited with code 255") as error:
        _open(harness)

    assert "Host key verification failed." in str(error.value)
    assert terminal.disconnected is True
    assert harness.audit.records[-1]["event"] == "open_session"
    assert harness.audit.records[-1]["outcome"] == "failed"
    assert harness.audit.records[-1]["exit_code"] == 255
    assert "Host key verification failed." in str(harness.audit.records[-1]["error"])


def test_open_rejects_an_unsupported_command_before_creating_a_terminal() -> None:
    harness = Harness()
    with pytest.raises(CommandError, match="unsupported command"):
        _open(harness, "console 192.0.2.1")
    assert harness.parsed == []


def test_max_open_sessions_is_enforced_before_spawning() -> None:
    harness = Harness(limits=_limits(max_open_sessions=1))
    _open(harness)
    with pytest.raises(SessionError, match="maximum number of open sessions"):
        _open(harness)
    assert len(harness.parsed) == 1


def test_naturally_exited_session_does_not_block_a_new_one() -> None:
    first = FakeTerminal()
    second = FakeTerminal()
    harness = Harness([first, second], limits=_limits(max_open_sessions=1))
    first_info = _open(harness)
    connection = harness.manager._sessions[first_info.session_id].connection
    assert isinstance(connection, FakeTerminal)
    connection.alive = False
    connection.exit_code_value = 0

    second_info = _open(harness)

    assert second_info.state is SessionState.READY
    assert harness.manager.session_status(first_info.session_id).state is SessionState.CLOSED


def test_open_transport_failure_is_audited_and_cleaned_up() -> None:
    class FailingFactory(Harness):
        def _factory(self, parsed: ParsedCommand) -> TerminalConnection:
            raise TransportError("ssh executable was not found on PATH")

    harness = FailingFactory()
    with pytest.raises(TransportError, match="executable was not found"):
        _open(harness)
    assert harness.audit.records[-1]["outcome"] == "failed"
    assert "executable was not found" in str(harness.audit.records[-1]["error"])


# ---------------------------------------------------------------------------
# terminal_write
# ---------------------------------------------------------------------------


def test_terminal_write_appends_enter_and_audits_redacted_input() -> None:
    harness = Harness()
    info = _open(harness)
    # The terminal was consumed by the factory; retrieve it from the manager.
    session = harness.manager._sessions[info.session_id]
    fake = session.connection
    assert isinstance(fake, FakeTerminal)

    result = harness.manager.terminal_write(info.session_id, "display version")

    assert fake.writes == ["display version\n"]
    assert result.bytes_sent == len("display version\n")
    records = [r for r in harness.audit.records if r["event"] == "terminal_write"]
    assert records[0]["data"] == "display version"
    assert records[0]["enter"] is True


def test_terminal_write_without_enter_sends_control_bytes() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)

    harness.manager.terminal_write(info.session_id, "\x03", enter=False)
    harness.manager.terminal_write(info.session_id, " ", enter=False)

    assert fake.writes == ["\x03", " "]


def test_terminal_write_serial_uses_carriage_return() -> None:
    harness = Harness()
    info = _open(harness, "serial /dev/ttyUSB0 115200")
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    assert harness.parsed[0].device == "/dev/ttyUSB0"
    assert harness.parsed[0].baudrate == 115200

    harness.manager.terminal_write(info.session_id, "show version")

    assert fake.writes == ["show version\r"]


def test_terminal_write_rejects_oversized_input() -> None:
    harness = Harness(limits=_limits(max_write_bytes=5))
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)

    with pytest.raises(SessionError, match="max_write_bytes"):
        harness.manager.terminal_write(info.session_id, "show version")
    assert fake.writes == []


def test_write_failure_marks_session_failed_and_redacts_secret() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    harness.manager.terminal_write_secret(info.session_id, value="hunter2")
    fake.write_error = OSError("broken pipe with hunter2")

    with pytest.raises(TransportError, match="terminal write failed") as error:
        harness.manager.terminal_write(info.session_id, "show version")

    assert "hunter2" not in str(error.value)
    status = harness.manager.session_status(info.session_id)
    assert status.state is SessionState.FAILED
    with pytest.raises(SessionError, match="not ready"):
        harness.manager.terminal_write(info.session_id, "show version")
    assert "hunter2" not in json.dumps(harness.audit.records)


# ---------------------------------------------------------------------------
# terminal_read and read_output
# ---------------------------------------------------------------------------


def test_write_timeout_marks_session_failed_and_can_be_closed() -> None:
    class TimeoutTerminal(FakeTerminal):
        def write_channel(self, out_data: str) -> None:
            raise TransportError("terminal write timed out after 0.1s")

    harness = Harness([TimeoutTerminal()])
    info = _open(harness)

    with pytest.raises(TransportError, match="timed out"):
        harness.manager.terminal_write(info.session_id, "show version")

    assert harness.manager.session_status(info.session_id).state is SessionState.FAILED
    closed = harness.manager.close_session(info.session_id)
    assert closed.state is SessionState.CLOSED


def test_terminal_read_returns_output_and_buffers_it() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("hello\nswitch#")

    result = harness.manager.terminal_read(info.session_id, timeout=2.0)

    assert result.output == "hello\nswitch#"
    assert result.output_offset == 0
    assert result.truncated is False
    assert harness.manager.read_output(info.session_id, offset=0).output == (
        "hello\nswitch#"
    )


def test_terminal_read_truncates_large_inline_output() -> None:
    harness = Harness(limits=_limits(max_inline_output_bytes=8))
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("0123456789abcdef")

    result = harness.manager.terminal_read(info.session_id, timeout=2.0)

    assert result.truncated is True
    assert result.output == "01234567"
    assert result.next_output_offset == 8
    tail = harness.manager.read_output(info.session_id)
    assert tail.output == "89abcdef"
    assert tail.next_offset is None
    assert harness.manager.read_output(info.session_id).output == ""
    assert harness.manager.read_output(info.session_id, offset=8).output == "89abcdef"


def test_default_read_limit_keeps_unseen_64k_tail_without_replay() -> None:
    assert RuntimeLimits().max_inline_output_bytes == 65_536
    harness = Harness(
        limits=_limits(max_inline_output_bytes=65_536, max_session_buffer_bytes=100_000)
    )
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("x" * 65_536 + "tail")

    first = harness.manager.terminal_read(info.session_id)
    second = harness.manager.terminal_read(info.session_id)
    third = harness.manager.read_output(info.session_id)

    assert first.output == "x" * 65_536
    assert first.truncated is True
    assert first.next_output_offset == 65_536
    assert second.output == "tail"
    assert second.truncated is False
    assert third.output == ""


def test_read_output_continues_from_cursor_without_repeating() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("AAAA")

    assert harness.manager.terminal_read(info.session_id, timeout=1.0).output == "AAAA"
    assert harness.manager.read_output(info.session_id).output == ""

    fake.outputs.append("BBBB")
    assert harness.manager.terminal_read(info.session_id, timeout=1.0).output == "BBBB"
    assert harness.manager.read_output(info.session_id).output == ""

    history = harness.manager.read_output(info.session_id, offset=0)
    assert history.output == "AAAABBBB"
    assert harness.manager.read_output(info.session_id).output == ""


def test_read_output_is_audited_with_offsets() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("AAAA")
    harness.manager.terminal_read(info.session_id, timeout=1.0)

    harness.manager.read_output(info.session_id)

    records = [r for r in harness.audit.records if r["event"] == "read_output"]
    assert records[-1]["offset"] == 4
    assert records[-1]["bytes"] == 0
    assert records[-1]["cursor"] == 4


def test_terminal_read_validates_timeout_bounds() -> None:
    harness = Harness(limits=_limits(max_read_timeout=10))
    info = _open(harness)

    with pytest.raises(SessionError, match="positive"):
        harness.manager.terminal_read(info.session_id, timeout=0)
    with pytest.raises(SessionError, match="max_read_timeout"):
        harness.manager.terminal_read(info.session_id, timeout=11)


def test_terminal_read_failure_marks_the_session_failed() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.read_error = OSError("read failed")

    with pytest.raises(TransportError, match="terminal read failed"):
        harness.manager.terminal_read(info.session_id)

    assert harness.manager.session_status(info.session_id).state is SessionState.FAILED


# ---------------------------------------------------------------------------
# terminal_write_secret
# ---------------------------------------------------------------------------


def test_secret_value_is_written_redacted_and_not_audited() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)

    result = harness.manager.terminal_write_secret(info.session_id, value="typed-secret")

    assert fake.writes == ["typed-secret\n"]
    assert result.source == "value"
    assert result.bytes_sent == len("typed-secret\n")
    assert "typed-secret" not in result.model_dump_json()
    assert "typed-secret" not in json.dumps(harness.audit.records)

    fake.outputs.append("got typed-secret")
    read = harness.manager.terminal_read(info.session_id)
    assert read.output == "got <redacted>"


def test_secret_env_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVICE_PASSWORD", "env-secret")
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)

    result = harness.manager.terminal_write_secret(info.session_id, env="DEVICE_PASSWORD")

    assert fake.writes == ["env-secret\n"]
    assert result.source == "env"
    assert "DEVICE_PASSWORD" not in json.dumps(harness.audit.records)
    records = [r for r in harness.audit.records if r["event"] == "terminal_write_secret"]
    assert records[-1]["source"] == "env"


def test_secret_file_source_strips_one_newline(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("file-secret\r\n", encoding="utf-8")
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)

    result = harness.manager.terminal_write_secret(
        info.session_id, file=str(secret_file)
    )

    assert fake.writes == ["file-secret\n"]
    assert result.source == "file"
    encoded_audit = json.dumps(harness.audit.records)
    assert "file-secret" not in encoded_audit
    assert str(secret_file) not in encoded_audit


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one"),
        ({"value": "a", "env": "X"}, "exactly one"),
        ({"value": ""}, "empty"),
        ({"value": "   "}, "empty"),
        ({"env": "NETWORK_TERMINAL_MISSING_SECRET"}, "not set"),
        ({"file": "/missing/secret/file"}, "not readable"),
    ],
)
def test_invalid_secret_sources_are_rejected(
    kwargs: dict[str, str], message: str
) -> None:
    harness = Harness()
    info = _open(harness)
    with pytest.raises(CredentialError, match=message):
        harness.manager.terminal_write_secret(info.session_id, **kwargs)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_close_session_disconnects_and_retains_status() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)

    closed = harness.manager.close_session(info.session_id)

    assert closed.state is SessionState.CLOSED
    assert fake.disconnected is True
    assert harness.manager.session_status(info.session_id).state is SessionState.CLOSED
    assert harness.manager.close_session(info.session_id).state is SessionState.CLOSED
    with pytest.raises(SessionError, match="not ready"):
        harness.manager.terminal_write(info.session_id, "show version")


def test_natural_zero_exit_becomes_closed_and_keeps_output_readable() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.alive = False
    fake.exit_code_value = 0
    fake.outputs.append("final output")

    assert harness.manager.session_status(info.session_id).state is SessionState.CLOSED
    assert harness.manager.terminal_read(info.session_id).output == "final output"
    with pytest.raises(SessionError, match="not ready: closed"):
        harness.manager.terminal_write(info.session_id, "show version")


def test_natural_nonzero_exit_becomes_failed_with_exit_code() -> None:
    harness = Harness()
    info = _open(harness)
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.alive = False
    fake.exit_code_value = 255

    status = harness.manager.session_status(info.session_id)

    assert status.state is SessionState.FAILED
    assert status.exit_code == 255
    assert status.error == "process exited with code 255"


def test_read_output_rejects_expired_offsets() -> None:
    harness = Harness(limits=_limits(max_session_buffer_bytes=8))
    info = _open(harness, "ssh operator@192.0.2.1")
    fake = harness.manager._sessions[info.session_id].connection
    assert isinstance(fake, FakeTerminal)
    fake.outputs.append("abcdefghij")
    harness.manager.terminal_read(info.session_id, timeout=1.0)

    with pytest.raises(SessionError, match="expired"):
        harness.manager.read_output(info.session_id, offset=0)
    chunk = harness.manager.read_output(info.session_id, offset=2)
    assert chunk.output == "cdefghij"
    assert chunk.truncated is True


def test_named_readers_are_independent_and_releasable() -> None:
    terminal = FakeTerminal(outputs=["abcdef"])
    harness = Harness([terminal], limits=_limits(max_output_readers=2))
    sid = harness.open().session_id
    assert harness.manager.read_output(sid, reader_id="one", limit=2).output == "ab"
    assert harness.manager.read_output(sid, reader_id="two", limit=3).output == "abc"
    assert harness.manager.terminal_read(sid).output == "abcdef"
    assert harness.manager.read_output(sid, reader_id="one").output == "cdef"
    assert harness.manager.read_output(sid, reader_id="two").output == "def"
    with pytest.raises(SessionError, match="maximum number of output readers"):
        harness.manager.read_output(sid, reader_id="three")
    assert harness.manager.release_output_reader(sid, "one") is True
    assert harness.manager.release_output_reader(sid, "one") is False
    assert harness.manager.read_output(sid, reader_id="three").output == "abcdef"


def test_named_reader_reports_eviction_and_can_resync() -> None:
    terminal = FakeTerminal(outputs=["abcd"])
    harness = Harness([terminal], limits=_limits(max_session_buffer_bytes=4))
    sid = harness.open().session_id
    assert harness.manager.read_output(sid, reader_id="slow", limit=1).output == "a"
    assert harness.manager.terminal_read(sid).output == "abcd"
    terminal.outputs.append("efgh")
    assert harness.manager.terminal_read(sid).output == "efgh"
    with pytest.raises(SessionError, match="expired"):
        harness.manager.read_output(sid, reader_id="slow")
    assert harness.manager.read_output(sid, reader_id="slow", offset=4).output == "efgh"
    assert harness.manager.read_output(sid, reader_id="slow").output == ""


class FakeBinaryTerminal(FakeTerminal):
    def __init__(self) -> None:
        super().__init__()
        self.binary_writes: list[bytes] = []

    def binary_ready(self) -> bool:
        return True

    def read_bytes(self, size: int, timeout: float) -> bytes:
        return b"binary-reply"[:size]

    def write_bytes(self, data: bytes) -> int:
        self.binary_writes.append(data)
        return len(data)


def test_binary_exclusive_complete_and_audit_has_no_payload() -> None:
    terminal = FakeBinaryTerminal()
    harness = Harness([terminal])
    sid = harness.open().session_id
    with harness.manager.exclusive_binary(sid) as channel:
        assert channel.write(b"binary-payload") == len(b"binary-payload")
        assert channel.read(100, 1) == b"binary-reply"
        with pytest.raises(SessionError, match="binary transfer"):
            harness.manager.terminal_write(sid, "show")
        channel.complete()
    assert harness.manager.session_status(sid).state is SessionState.READY
    assert "binary-payload" not in json.dumps(harness.audit.records)
    assert "binary-reply" not in json.dumps(harness.audit.records)
    with pytest.raises(SessionError, match="no longer available"):
        channel.write(b"late")


def test_binary_incomplete_transfer_fails_session() -> None:
    harness = Harness([FakeBinaryTerminal()])
    sid = harness.open().session_id
    with harness.manager.exclusive_binary(sid) as channel:
        channel.write(b"payload")
    assert harness.manager.session_status(sid).state is SessionState.FAILED
    with pytest.raises(SessionError, match="not ready"):
        harness.manager.terminal_write(sid, "show")
    harness.manager.close_session(sid)


def test_shutdown_closes_all_open_terminals() -> None:
    first = FakeTerminal()
    second = FakeTerminal()
    harness = Harness([first, second])
    first_info = _open(harness)
    second_info = _open(harness)

    harness.manager.shutdown()

    assert harness.manager.session_status(first_info.session_id).state is SessionState.CLOSED
    assert harness.manager.session_status(second_info.session_id).state is SessionState.CLOSED
    assert first.disconnected is True
    assert second.disconnected is True


def test_unknown_session_has_a_specific_error() -> None:
    harness = Harness()
    with pytest.raises(SessionError, match="unknown session"):
        harness.manager.session_status("missing")
