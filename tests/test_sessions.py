"""Unit tests for direct SSH session management."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import (
    ConnectionsConfig,
    CredentialsConfig,
    InventoryConfig,
    PolicyConfig,
)
from network_terminal_mcp.credentials.pass_backend import Credentials
from network_terminal_mcp.errors import PolicyError, SessionError, TransportError
from network_terminal_mcp.host_keys import HostKeyStatus
from network_terminal_mcp.sessions import SessionManager, SessionState


class FakeConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.expect_patterns: list[str] = []
        self.writes: list[str] = []
        self.command_outputs: dict[str, str] = {}
        self.read_outputs: list[str] = []
        self.timing_output = ""
        self.read_error: Exception | None = None
        self.disconnected = False

    def find_prompt(self) -> str:
        return "switch#"

    def send_command(
        self,
        command_string: str,
        *,
        expect_string: str | None = None,
        read_timeout: float,
        strip_prompt: bool = True,
        strip_command: bool = True,
        cmd_verify: bool = True,
    ) -> str:
        self.commands.append(command_string)
        if expect_string is not None:
            self.expect_patterns.append(expect_string)
        if command_string in self.command_outputs:
            return self.command_outputs[command_string]
        if command_string == "show long":
            return "x" * 40 + "switch#"
        return f"output for {command_string}switch#"

    def write_channel(self, out_data: str) -> None:
        self.writes.append(out_data)

    def read_until_pattern(self, pattern: str, *, read_timeout: float) -> str:
        if self.read_error is not None:
            raise self.read_error
        if self.read_outputs:
            return self.read_outputs.pop(0)
        return "switch#"

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        return self.timing_output

    def disconnect(self) -> None:
        self.disconnected = True


class FakeCredentials:
    def resolve(self, profile: object) -> Credentials:
        return Credentials(username="operator", password="hunter2")


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


class FakeHostKeys:
    path = Path("/tmp/known_hosts")

    def ensure(
        self, host: str, *, port: int, policy: str, timeout: float
    ) -> HostKeyStatus:
        return HostKeyStatus(
            host=host,
            port=port,
            algorithm="ssh-ed25519",
            fingerprint="SHA256:known",
            enrolled=False,
        )


class MutableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _config(**runtime: object) -> AppConfig:
    return AppConfig(
        inventory=InventoryConfig.model_validate(
            {
                "devices": {
                    "sw1": {
                        "host": "192.0.2.1",
                        "platform": "snr_29xx",
                        "credentials": "net",
                        "connection": "direct",
                    }
                }
            }
        ),
        connections=ConnectionsConfig.model_validate(
            {
                "connections": {
                    "direct": {"type": "direct", "protocol": "ssh"}
                },
                "platforms": {
                    "snr_29xx": {"driver": "cisco_ios", "dialect": "snr_29xx"}
                },
            }
        ),
        credentials=CredentialsConfig.model_validate(
            {
                "credentials": {
                    "net": {"backend": "pass", "entry": "network/net", "username": "operator"}
                }
            }
        ),
        policy=PolicyConfig.model_validate(
            {
                "rules": [{"id": "show", "action": "allow", "command_patterns": ["show *"]}],
                "runtime": runtime,
            }
        ),
        config_dir=Path("."),
    )


def _manager(
    connection: FakeConnection,
    audit: FakeAudit,
    clock: MutableClock | None = None,
    **runtime: object,
) -> SessionManager:
    return SessionManager(
        _config(**runtime),
        connection_factory=lambda params: connection,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )


def test_open_session_resolves_alias_and_uses_factory() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    assert info.state is SessionState.READY
    assert info.dialect == "snr_29xx"
    assert info.prompt == "switch#"


def test_allowed_command_is_executed_and_audited() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")
    result = manager.run_command(info.session_id, "show version")
    assert result.executed is True
    assert result.output == "output for show version"
    assert connection.commands == ["show version"]
    assert audit.records[-1]["outcome"] == "completed"


def test_ask_command_does_not_execute() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    result = manager.run_command(info.session_id, "ping 192.0.2.1")
    assert result.executed is False
    assert result.confirmation_required is True
    assert connection.commands == []


def test_denied_command_raises_without_execution() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    with pytest.raises(PolicyError):
        manager.run_command(info.session_id, "reload")
    assert connection.commands == []


def test_run_commands_stops_at_first_unapproved_command() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    results = manager.run_commands(
        info.session_id, ["show version", "ping 192.0.2.1", "show clock"]
    )
    assert [result.executed for result in results] == [True, False]
    assert connection.commands == ["show version"]


def test_cli_help_reads_completion_and_cancels_unfinished_line() -> None:
    connection = FakeConnection()
    connection.timing_output = "  version  Show software version\n"
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.cli_help(info.session_id, "show ")

    assert result.executed is True
    assert result.output == "  version  Show software version\n"
    assert connection.commands == []
    assert connection.writes == ["show ?", "\x03"]
    assert manager.session_status(info.session_id).state is SessionState.READY
    assert audit.records[-1]["event"] == "cli_help"
    assert audit.records[-1]["outcome"] == "completed"


@pytest.mark.parametrize("line", ["show ?", "show version\n", "show $(hostname)"])
def test_cli_help_rejects_unsafe_or_ambiguous_input(line: str) -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(PolicyError):
        manager.cli_help(info.session_id, line)

    assert connection.writes == []


def test_cli_help_fails_the_session_when_cleanup_cannot_restore_prompt() -> None:
    connection = FakeConnection()
    connection.timing_output = "  version  Show software version\n"
    connection.read_error = RuntimeError("prompt not received")
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(TransportError, match="prompt not received"):
        manager.cli_help(info.session_id, "show ")

    assert connection.writes == ["show ?", "\x03"]
    assert manager.session_status(info.session_id).state is SessionState.FAILED


def test_pager_requires_explicit_space_and_returns_to_ready() -> None:
    connection = FakeConnection()
    connection.command_outputs["show interfaces"] = "first page\n--More--"
    connection.read_outputs = ["second page\nswitch#"]
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    first = manager.run_command(info.session_id, "show interfaces")

    assert first.pager_active is True
    assert first.output == "first page\n--More--"
    assert re.search(connection.expect_patterns[-1], "first page\n--More--")
    assert manager.session_status(info.session_id).state is SessionState.PAGING

    second = manager.send_control(info.session_id, "space")

    assert second.action == "space"
    assert second.output == "second page\n"
    assert second.pager_active is False
    assert connection.writes == [" "]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_pager_limit_aborts_with_q() -> None:
    connection = FakeConnection()
    connection.command_outputs["show interfaces"] = "first page\n--More--"
    manager = _manager(connection, FakeAudit(), max_pager_pages=1)
    info = manager.open_session("sw1")
    manager.run_command(info.session_id, "show interfaces")

    result = manager.send_control(info.session_id, "space")

    assert result.action == "q"
    assert connection.writes == ["q"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_q_cancels_an_active_pager() -> None:
    connection = FakeConnection()
    connection.command_outputs["show interfaces"] = "first page\n--More--"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.run_command(info.session_id, "show interfaces")

    result = manager.send_control(info.session_id, "q")

    assert result.action == "q"
    assert connection.writes == ["q"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_confirmation_only_accepts_detected_response_tokens() -> None:
    connection = FakeConnection()
    connection.command_outputs["show version"] = "Continue? [Y/N]"
    connection.command_outputs["y"] = "continued\nswitch#"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    command = manager.run_command(info.session_id, "show version")

    assert command.response_required is True
    assert command.allowed_responses == ["y", "n"]
    assert manager.session_status(info.session_id).state is SessionState.AWAITING_RESPONSE
    with pytest.raises(PolicyError):
        manager.respond(info.session_id, "yes")
    assert connection.commands == ["show version"]

    response = manager.respond(info.session_id, "Y")

    assert response.response == "y"
    assert response.output == "continued\n"
    assert connection.commands == ["show version", "y"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_ctrl_c_cancels_a_pending_confirmation() -> None:
    connection = FakeConnection()
    connection.command_outputs["show version"] = "Continue? [Y/N]"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.run_command(info.session_id, "show version")

    result = manager.send_control(info.session_id, "ctrl-c")

    assert result.action == "ctrl-c"
    assert connection.writes == ["\x03"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_plain_y_n_text_is_not_treated_as_a_confirmation_prompt() -> None:
    connection = FakeConnection()
    connection.command_outputs["show version"] = "answer column [Y/N]\nswitch#"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    result = manager.run_command(info.session_id, "show version")

    assert result.response_required is False
    assert result.output == "answer column [Y/N]\n"
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_unknown_confirmation_prompt_fails_without_sending_a_response() -> None:
    connection = FakeConnection()
    connection.command_outputs["show version"] = "Continue? [OK/CANCEL]"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(TransportError, match="did not end at a recognized prompt"):
        manager.run_command(info.session_id, "show version")

    assert connection.commands == ["show version"]
    assert connection.writes == []
    assert manager.session_status(info.session_id).state is SessionState.FAILED


def test_secret_prompt_fails_session_without_sending_a_response() -> None:
    connection = FakeConnection()
    connection.command_outputs["show version"] = "Password:"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(TransportError, match="requested a password"):
        manager.run_command(info.session_id, "show version")

    assert connection.commands == ["show version"]
    assert manager.session_status(info.session_id).state is SessionState.FAILED


def test_control_is_rejected_when_no_pager_or_prompt_is_pending() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(SessionError, match="requires paging"):
        manager.send_control(info.session_id, "space")

    assert connection.writes == []


def test_read_output_is_chunked() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.run_command(info.session_id, "show version")
    first = manager.read_output(info.session_id, limit=6)
    second = manager.read_output(info.session_id, offset=first.next_offset or 0, limit=100)
    assert first.output == "output"
    assert second.output == " for show version"


def test_inline_output_limit_marks_result_truncated() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), max_inline_output_bytes=10)
    info = manager.open_session("sw1")
    result = manager.run_command(info.session_id, "show long")
    assert result.truncated is True
    assert result.output == "x" * 10
    assert result.next_output_offset is not None


def test_expired_session_is_disconnected() -> None:
    connection = FakeConnection()
    clock = MutableClock()
    manager = _manager(connection, FakeAudit(), clock, session_idle_timeout=5)
    info = manager.open_session("sw1")
    clock.value = 6
    with pytest.raises(SessionError, match="unknown or expired"):
        manager.session_status(info.session_id)
    assert connection.disconnected is True


def test_close_removes_session() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    closed = manager.close_session(info.session_id)
    assert closed.state is SessionState.CLOSED
    assert connection.disconnected is True
    with pytest.raises(SessionError):
        manager.session_status(info.session_id)
