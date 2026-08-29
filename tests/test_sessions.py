"""Unit tests for direct SSH session management."""

from __future__ import annotations

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
from network_terminal_mcp.errors import PolicyError, SessionError
from network_terminal_mcp.host_keys import HostKeyStatus
from network_terminal_mcp.sessions import SessionManager, SessionState


class FakeConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.disconnected = False

    def find_prompt(self) -> str:
        return "switch#"

    def send_command(self, command_string: str, *, read_timeout: float) -> str:
        self.commands.append(command_string)
        if command_string == "show long":
            return "x" * 40
        return f"output for {command_string}"

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
