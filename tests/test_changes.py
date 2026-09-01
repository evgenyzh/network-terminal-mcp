"""Unit tests for configuration change plan lifecycle."""

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
from network_terminal_mcp.errors import ChangeError, PolicyError, SessionError
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


def test_plan_change_creates_a_proposed_plan_without_executing() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")

    result = manager.plan_change(
        info.session_id,
        "add access-list",
        ["access-list 100 permit ip 192.0.2.0/24 any"],
    )

    assert result.state.value == "proposed"
    assert result.confirmation_required is False
    assert result.hash
    assert connection.commands == []
    assert audit.records[-1]["event"] == "plan_change"
    assert audit.records[-1]["outcome"] == "created"


def test_plan_change_rejects_empty_title_and_commands() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(ChangeError, match="title"):
        manager.plan_change(info.session_id, "  ", ["show version"])
    with pytest.raises(ChangeError, match="at least one command"):
        manager.plan_change(info.session_id, "t", [])


def test_plan_change_rejects_structural_hazards() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(PolicyError, match="command chaining"):
        manager.plan_change(info.session_id, "t", ["show version; reload"])


def test_apply_change_is_two_step_and_executes_only_the_canonical_commands() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")
    plan = manager.plan_change(
        info.session_id,
        "add access-list",
        [
            "configure terminal",
            "access-list 100 permit ip 192.0.2.0/24 any",
        ],
    )

    first = manager.apply_change(plan.change_id)
    assert first.confirmation_required is True
    assert first.state.value == "confirmed"
    assert connection.commands == []

    second = manager.apply_change(plan.change_id)
    assert second.state.value == "applied"
    assert second.confirmation_required is False
    assert connection.commands == [
        "configure terminal",
        "access-list 100 permit ip 192.0.2.0/24 any",
    ]
    assert "output for configure terminal" in second.output
    assert audit.records[-1]["event"] == "apply_change"
    assert audit.records[-1]["outcome"] == "completed"


def test_apply_change_cannot_be_reapplied() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    plan = manager.plan_change(info.session_id, "t", ["configure terminal"])
    manager.apply_change(plan.change_id)
    manager.apply_change(plan.change_id)

    with pytest.raises(ChangeError, match="cannot be applied"):
        manager.apply_change(plan.change_id)


def test_abort_change_prevents_execution() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")
    plan = manager.plan_change(info.session_id, "t", ["configure terminal"])

    result = manager.abort_change(plan.change_id)

    assert result.state.value == "aborted"
    assert connection.commands == []
    assert audit.records[-1]["event"] == "abort_change"

    with pytest.raises(ChangeError, match="cannot be applied"):
        manager.apply_change(plan.change_id)


def test_finalize_change_marks_applied_plan_finalized() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")
    plan = manager.plan_change(info.session_id, "t", ["vlan 100"])
    manager.apply_change(plan.change_id)
    manager.apply_change(plan.change_id)

    finalized = manager.finalize_change(plan.change_id)

    assert finalized.state.value == "finalized"
    assert connection.commands == ["vlan 100"]
    assert audit.records[-1]["event"] == "finalize_change"
    assert audit.records[-1]["outcome"] == "completed"

    with pytest.raises(ChangeError, match="cannot be finalized"):
        manager.finalize_change(plan.change_id)


def test_auto_approve_skips_the_confirmation_step() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    plan = manager.plan_change(
        info.session_id,
        "t",
        ["configure terminal"],
        auto_approve=True,
    )

    result = manager.apply_change(plan.change_id)

    assert result.state.value == "applied"
    assert result.confirmation_required is False
    assert connection.commands == ["configure terminal"]


def test_plan_change_requires_a_ready_session() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.close_session(info.session_id)

    with pytest.raises(SessionError):
        manager.plan_change(info.session_id, "t", ["show version"])


def test_apply_change_rejects_a_structural_hazard_in_a_command() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    with pytest.raises(PolicyError, match="command chaining"):
        manager.plan_change(info.session_id, "t", ["vlan 100; rm -rf /"])


def test_apply_change_fails_closed_when_a_command_enters_a_pager() -> None:
    connection = FakeConnection()
    connection.command_outputs["show run"] = "line1\n--More--\n"
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")
    plan = manager.plan_change(info.session_id, "t", ["show run"])
    manager.apply_change(plan.change_id)

    result = manager.apply_change(plan.change_id)

    assert result.state.value == "failed"
    assert result.error is not None
    assert any(record["event"] == "apply_change" and record["outcome"] == "failed"
               for record in audit.records)


def test_apply_change_fails_closed_when_a_command_triggers_confirmation() -> None:
    connection = FakeConnection()
    connection.command_outputs["delete file"] = (
        "Are you sure you want to delete? [y/n]:"
    )
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = manager.open_session("sw1")
    plan = manager.plan_change(info.session_id, "t", ["delete file"])
    manager.apply_change(plan.change_id)

    result = manager.apply_change(plan.change_id)

    assert result.state.value == "failed"
    assert any(record["event"] == "apply_change" and record["outcome"] == "failed"
               for record in audit.records)
