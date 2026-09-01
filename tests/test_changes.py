"""Unit tests for configuration change execution."""

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
from network_terminal_mcp.sessions import SessionManager


class FakeConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.writes: list[str] = []
        self.command_outputs: dict[str, str | Exception] = {}
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
        if command_string in self.command_outputs:
            value = self.command_outputs[command_string]
            if isinstance(value, Exception):
                raise value
            return value
        return f"output for {command_string}switch#"

    def write_channel(self, out_data: str) -> None:
        self.writes.append(out_data)

    def read_until_pattern(self, pattern: str, *, read_timeout: float) -> str:
        return "switch#"

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        return ""

    def disconnect(self) -> None:
        self.disconnected = True


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


def _config() -> AppConfig:
    return AppConfig(
        inventory=InventoryConfig.model_validate(
            {
                "devices": {
                    "sw1": {
                        "host": "192.0.2.1",
                        "credentials": "net",
                        "connection": "direct",
                    }
                }
            }
        ),
        connections=ConnectionsConfig.model_validate(
            {
                "connections": {"direct": {"type": "direct", "protocol": "ssh"}},
            }
        ),
        credentials=CredentialsConfig.model_validate(
            {
                "credentials": {
                    "net": {"backend": "pass", "entry": "network/net", "username": "operator"},
                }
            }
        ),
        policy=PolicyConfig(),
        config_dir=Path("."),
    )


class FakeCredentials:
    def resolve(self, profile: object) -> Credentials:
        return Credentials(username="operator", password="hunter2")


class FakeHostKeys:
    path = Path("/tmp/known_hosts")

    def ensure(
        self,
        host: str,
        *,
        port: int,
        policy: str,
        timeout: float,
        probe: object | None = None,
    ) -> object:
        return None


def _manager(
    connection: FakeConnection,
    audit: FakeAudit,
) -> SessionManager:
    return SessionManager(
        _config(),
        connection_factory=lambda params: connection,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )


def test_run_change_executes_all_commands() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.run_change(
        info.session_id,
        [
            "configure terminal",
            "access-list 100 permit ip 192.0.2.0/24 any",
        ],
    )

    assert result.error is None
    assert [command.command for command in result.commands] == [
        "configure terminal",
        "access-list 100 permit ip 192.0.2.0/24 any",
    ]
    assert all(command.executed for command in result.commands)
    assert connection.commands == [
        "configure terminal",
        "access-list 100 permit ip 192.0.2.0/24 any",
    ]
    assert "output for configure terminal" in result.output
    assert audit.records[-1]["event"] == "run_change"
    assert audit.records[-1]["outcome"] == "completed"


def test_run_change_rejects_empty_commands() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(PolicyError, match="at least one command"):
        manager.run_change(info.session_id, [])

    assert connection.commands == []


def test_run_change_rejects_structural_hazards_before_executing() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(PolicyError, match="command chaining"):
        manager.run_change(info.session_id, ["show version; reload"])

    assert connection.commands == []


def test_run_change_requires_a_ready_session() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.close_session(info.session_id)

    with pytest.raises(SessionError):
        manager.run_change(info.session_id, ["show version"])


def test_run_change_rejects_a_structural_hazard_in_a_command() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(PolicyError, match="command chaining"):
        manager.run_change(info.session_id, ["vlan 100; rm -rf /"])

    assert connection.commands == []


def test_run_change_fails_closed_when_a_command_enters_a_pager() -> None:
    connection = FakeConnection()
    connection.command_outputs["show run"] = "line1\n--More--\n"
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.run_change(info.session_id, ["show run"])

    assert result.error is not None
    assert result.commands[0].executed is False
    assert any(record["event"] == "run_change" and record["outcome"] == "failed"
               for record in audit.records)


def test_run_change_fails_closed_when_a_command_triggers_confirmation() -> None:
    connection = FakeConnection()
    connection.command_outputs["delete file"] = (
        "Are you sure you want to delete? [y/n]:"
    )
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.run_change(info.session_id, ["delete file"])

    assert result.error is not None
    assert result.commands[0].executed is False
    assert any(record["event"] == "run_change" and record["outcome"] == "failed"
               for record in audit.records)


def test_run_change_stops_at_the_first_failed_command() -> None:
    connection = FakeConnection()
    connection.command_outputs["configure terminal"] = (
        "Are you sure? [y/n]:"
    )
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.run_change(
        info.session_id,
        ["configure terminal", "interface FastEthernet0/13"],
    )

    assert result.error is not None
    assert [command.command for command in result.commands] == [
        "configure terminal",
        "interface FastEthernet0/13",
    ]
    assert result.commands[0].executed is False
    assert result.commands[1].executed is False
    assert connection.commands == ["configure terminal"]


def test_run_change_records_a_failed_connection_error() -> None:
    connection = FakeConnection()
    connection.command_outputs["configure terminal"] = RuntimeError("socket closed")
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.run_change(info.session_id, ["configure terminal"])

    assert result.error is not None
    assert any(record["event"] == "run_change" and record["outcome"] == "failed"
               for record in audit.records)


def test_prompt_end_pattern_matches_sub_mode_prompts() -> None:
    import re

    from network_terminal_mcp.sessions import SessionManager

    class FakeSession:
        raw_prompt = "Krupskoy_16_5pod_u#"

    pattern = SessionManager._prompt_end_pattern(FakeSession())  # type: ignore[arg-type]
    assert re.search(pattern, "Krupskoy_16_5pod_u#") is not None
    assert re.search(pattern, "Krupskoy_16_5pod_u(config)#") is not None
    assert re.search(pattern, "Krupskoy_16_5pod_u(config-if)#") is not None

    class JunosSession:
        raw_prompt = "zz@BR2>"

    junos = SessionManager._prompt_end_pattern(JunosSession())  # type: ignore[arg-type]
    assert re.search(junos, "zz@BR2>") is not None
    assert re.search(junos, "zz@BR2#") is not None
