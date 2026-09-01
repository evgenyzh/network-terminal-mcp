"""Unit tests for direct SSH session management."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import (
    ConnectionsConfig,
    CredentialProfile,
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
        self.timing_error: Exception | None = None
        self.read_timeouts: list[float] = []
        self.timing_timeouts: list[float] = []
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
        self.read_timeouts.append(read_timeout)
        if self.read_error is not None:
            raise self.read_error
        if self.read_outputs:
            return self.read_outputs.pop(0)
        return "switch#"

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        self.timing_timeouts.append(read_timeout)
        if self.timing_error is not None:
            raise self.timing_error
        return self.timing_output

    def disconnect(self) -> None:
        self.disconnected = True


class FakeJumpTransport:
    def __init__(self) -> None:
        self.active = True
        self.channels: list[tuple[str, tuple[str, int], tuple[str, int], float]] = []

    def is_active(self) -> bool:
        return self.active

    def open_channel(
        self,
        kind: str,
        destination: tuple[str, int],
        source: tuple[str, int],
        *,
        timeout: float,
    ) -> object:
        self.channels.append((kind, destination, source, timeout))
        return object()


class FakeJumpClient:
    def __init__(self) -> None:
        self.transport = FakeJumpTransport()
        self.loaded_host_keys: list[str] = []
        self.missing_host_key_policy: object | None = None
        self.connect_kwargs: dict[str, object] = {}
        self.connect_error: Exception | None = None
        self.closed = False

    def load_host_keys(self, filename: str) -> None:
        self.loaded_host_keys.append(filename)

    def set_missing_host_key_policy(self, policy: object) -> None:
        self.missing_host_key_policy = policy

    def connect(self, hostname: str, **kwargs: object) -> None:
        self.connect_kwargs = {"hostname": hostname, **kwargs}
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self) -> FakeJumpTransport:
        return self.transport

    def close(self) -> None:
        self.closed = True


class FakeCredentials:
    def resolve(self, profile: CredentialProfile) -> Credentials:
        if profile.backend == "ssh_key":
            return Credentials(username=profile.username, key_file=str(profile.key_file))
        if profile.username == "term-operator":
            password = "intermediate-secret"
        elif profile.username == "jump-operator":
            password = "jump-secret"
        else:
            password = "hunter2"
        return Credentials(username=profile.username, password=password)


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


class FakeHostKeys:
    path = Path("/tmp/known_hosts")

    def __init__(self, *, invoke_probe: bool = False) -> None:
        self.calls: list[tuple[str, int, str, bool]] = []
        self.invoke_probe = invoke_probe

    def ensure(
        self,
        host: str,
        *,
        port: int,
        policy: str,
        timeout: float,
        probe: object | None = None,
    ) -> HostKeyStatus:
        self.calls.append((host, port, policy, probe is not None))
        if self.invoke_probe and probe is not None:
            probe(host, port, timeout)  # type: ignore[operator]
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


def _config(
    *,
    proxyjump: bool = False,
    socks: bool = False,
    jump_key: bool = False,
    nested: bool = False,
    nested_telnet: bool = False,
    telnet: bool = False,
    allow_telnet: bool = False,
    console: bool = False,
    **runtime: object,
) -> AppConfig:
    if socks:
        connection_name = "through-socks"
    elif proxyjump:
        connection_name = "through-jump"
    elif nested or nested_telnet:
        connection_name = "nested-term"
    elif console:
        connection_name = "console"
    elif telnet:
        connection_name = "direct-telnet"
    else:
        connection_name = "direct"
    connections: dict[str, object] = {
        "direct": {"type": "direct", "protocol": "ssh"}
    }
    credentials: dict[str, object] = {
        "net": {"backend": "pass", "entry": "network/net", "username": "operator"}
    }
    if socks:
        connections["through-socks"] = {
            "type": "proxyjump",
            "socks": {"host": "127.0.0.1", "port": 10900},
            "host_key_policy": "accept_new",
        }
    if proxyjump:
        connections["through-jump"] = {
            "type": "proxyjump",
            "jump_host": "192.0.2.254",
            "jump_credentials": "jump",
        }
        credentials["jump"] = (
            {
                "backend": "ssh_key",
                "key_file": "/tmp/jump-key",
                "username": "jump-operator",
            }
            if jump_key
            else {
                "backend": "pass",
                "entry": "network/jump",
                "username": "jump-operator",
            }
        )
    if nested or nested_telnet:
        connections["nested-term"] = {
            "type": "nested",
            "host": "192.0.2.254",
            "protocol": "ssh",
            "credentials": "intermediate",
            "next_protocol": "telnet" if nested_telnet else "ssh",
        }
        credentials["intermediate"] = {
            "backend": "pass",
            "entry": "network/intermediate",
            "username": "term-operator",
        }
    if telnet:
        connections["direct-telnet"] = {
            "type": "direct",
            "protocol": "telnet",
            "host_key_policy": "strict",
        }
    if console:
        connections["console"] = {
            "type": "console",
            "port": 2002,
        }
    inventory_devices = {
        "sw1": {
            "host": "192.0.2.1",
            "credentials": "net",
            "connection": connection_name,
        }
    }
    if allow_telnet:
        inventory_devices["sw1"]["allow_telnet"] = True  # type: ignore[index]
    policy_data: dict[str, object] = {
        "rules": [{"id": "show", "action": "allow", "command_patterns": ["show *"]}],
        "runtime": runtime,
    }
    if telnet or nested_telnet or console:
        policy_data["defaults"] = {"telnet": "allow"}
    return AppConfig(
        inventory=InventoryConfig.model_validate(
            {"devices": inventory_devices}
        ),
        connections=ConnectionsConfig.model_validate(
            {"connections": connections}
        ),
        credentials=CredentialsConfig.model_validate(
            {
                "credentials": credentials
            }
        ),
        policy=PolicyConfig.model_validate(policy_data),
        config_dir=Path("."),
    )


def _manager(
    connection: FakeConnection,
    audit: FakeAudit,
    clock: MutableClock | None = None,
    *,
    allow_telnet: bool = False,
    telnet: bool = False,
    console: bool = False,
    **runtime: object,
) -> SessionManager:
    return SessionManager(
        _config(
            allow_telnet=allow_telnet,
            telnet=telnet,
            console=console,
            **runtime,
        ),
        connection_factory=lambda params: connection,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )


def _proxy_manager(
    connection: FakeConnection,
    jump_client: FakeJumpClient,
    audit: FakeAudit,
    *,
    connection_error: Exception | None = None,
    clock: MutableClock | None = None,
    host_keys: FakeHostKeys | None = None,
    jump_key: bool = False,
) -> tuple[SessionManager, FakeHostKeys, list[dict[str, object]]]:
    resolved_host_keys = host_keys or FakeHostKeys()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        if connection_error is not None:
            raise connection_error
        return connection

    manager = SessionManager(
        _config(proxyjump=True, jump_key=jump_key),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=resolved_host_keys,  # type: ignore[arg-type]
        jump_client_factory=lambda: jump_client,  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )
    return manager, resolved_host_keys, params


def _socks_manager(
    connection: FakeConnection,
    audit: FakeAudit,
    *,
    connection_error: Exception | None = None,
    clock: MutableClock | None = None,
    host_keys: FakeHostKeys | None = None,
) -> tuple[SessionManager, FakeHostKeys, list[dict[str, object]]]:
    resolved_host_keys = host_keys or FakeHostKeys()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        if connection_error is not None:
            raise connection_error
        return connection

    manager = SessionManager(
        _config(socks=True),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=resolved_host_keys,  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )
    return manager, resolved_host_keys, params


def _nested_manager(
    connection: FakeConnection,
    audit: FakeAudit,
    *,
    connection_error: Exception | None = None,
    host_keys: FakeHostKeys | None = None,
    clock: MutableClock | None = None,
    nested_telnet: bool = False,
) -> tuple[SessionManager, FakeHostKeys, list[dict[str, object]]]:
    resolved_host_keys = host_keys or FakeHostKeys()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        if connection_error is not None:
            raise connection_error
        return connection

    manager = SessionManager(
        _config(nested=True, nested_telnet=nested_telnet, allow_telnet=True),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=resolved_host_keys,  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )
    return manager, resolved_host_keys, params



def test_open_session_resolves_alias_and_uses_factory() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    assert info.state is SessionState.READY
    assert info.prompt == "switch#"


def test_proxyjump_uses_a_forwarded_socket_and_closes_the_jump_client() -> None:
    connection = FakeConnection()
    jump_client = FakeJumpClient()
    audit = FakeAudit()
    manager, host_keys, params = _proxy_manager(connection, jump_client, audit)

    info = manager.open_session("sw1")

    assert host_keys.calls == [
        ("192.0.2.254", 22, "strict", False),
        ("192.0.2.1", 22, "strict", True),
    ]
    assert jump_client.connect_kwargs == {
        "hostname": "192.0.2.254",
        "port": 22,
        "username": "jump-operator",
        "password": "jump-secret",
        "timeout": 60.0,
        "banner_timeout": 60.0,
        "auth_timeout": 60.0,
        "look_for_keys": False,
        "allow_agent": False,
    }
    assert jump_client.transport.channels == [
        ("direct-tcpip", ("192.0.2.1", 22), ("127.0.0.1", 0), 60.0)
    ]
    assert params[0]["host"] == "192.0.2.1"
    assert "sock" in params[0]
    assert audit.records[0]["route"] == "proxyjump"

    manager.close_session(info.session_id)

    assert connection.disconnected is True
    assert jump_client.closed is True


class FakeSocket:
    def settimeout(self, value: float) -> None:
        self.timeout = value

    def fileno(self) -> int:
        return -1


def test_socks_proxyjump_reaches_target_through_a_socks_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    host_keys = FakeHostKeys(invoke_probe=False)
    socks_targets: list[tuple[str, int, float]] = []
    fake_socket = FakeSocket()

    def fake_socks5_connect(
        proxy_host: str,
        proxy_port: int,
        target_host: str,
        target_port: int,
        timeout: float,
    ) -> object:
        socks_targets.append((target_host, target_port, timeout))
        assert proxy_host == "127.0.0.1"
        assert proxy_port == 10900
        return fake_socket

    monkeypatch.setattr(
        "network_terminal_mcp.sessions.manager.socks5_connect", fake_socks5_connect
    )
    manager, _, params = _socks_manager(connection, audit, host_keys=host_keys)

    info = manager.open_session("sw1")

    assert host_keys.calls == [("192.0.2.1", 22, "accept_new", True)]
    assert socks_targets == [(("192.0.2.1", 22, 60.0))]
    assert params[0]["host"] == "192.0.2.1"
    assert params[0]["sock"] is fake_socket
    assert audit.records[0]["route"] == "socks"

    manager.close_session(info.session_id)
    assert connection.disconnected is True


def test_socks_proxyjump_does_not_connect_an_ssh_bastion() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager, _, params = _socks_manager(connection, audit)

    info = manager.open_session("sw1")

    assert params[0]["sock"] is not None
    # No jump client is created for the socks-only route.
    assert "jump_client" not in params[0]

    manager.close_session(info.session_id)
    connection = FakeConnection()
    jump_client = FakeJumpClient()
    manager, _, _ = _proxy_manager(
        connection,
        jump_client,
        FakeAudit(),
        jump_key=True,
    )

    info = manager.open_session("sw1")

    assert jump_client.connect_kwargs == {
        "hostname": "192.0.2.254",
        "port": 22,
        "username": "jump-operator",
        "key_filename": "/tmp/jump-key",
        "passphrase": None,
        "timeout": 60.0,
        "banner_timeout": 60.0,
        "auth_timeout": 60.0,
        "look_for_keys": False,
        "allow_agent": False,
    }
    manager.close_session(info.session_id)


def test_proxyjump_probes_an_unenrolled_target_through_a_separate_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    jump_client = FakeJumpClient()
    host_keys = FakeHostKeys(invoke_probe=True)
    monkeypatch.setattr(
        "network_terminal_mcp.sessions.manager.probe_host_key_socket",
        lambda *_: object(),
    )
    manager, _, _ = _proxy_manager(
        connection,
        jump_client,
        FakeAudit(),
        host_keys=host_keys,
    )

    info = manager.open_session("sw1")

    assert len(jump_client.transport.channels) == 2
    manager.close_session(info.session_id)


def test_proxyjump_closes_the_jump_client_when_target_connection_fails() -> None:
    connection = FakeConnection()
    jump_client = FakeJumpClient()
    manager, _, _ = _proxy_manager(
        connection,
        jump_client,
        FakeAudit(),
        connection_error=RuntimeError("target login failed with hunter2 and jump-secret"),
    )

    with pytest.raises(TransportError, match="target login failed") as error:
        manager.open_session("sw1")

    assert "hunter2" not in str(error.value)
    assert "jump-secret" not in str(error.value)
    assert jump_client.closed is True


def test_proxyjump_closes_the_jump_client_when_bastion_login_fails() -> None:
    jump_client = FakeJumpClient()
    jump_client.connect_error = RuntimeError("bastion login failed with jump-secret")
    manager, _, _ = _proxy_manager(FakeConnection(), jump_client, FakeAudit())

    with pytest.raises(TransportError, match="bastion login failed") as error:
        manager.open_session("sw1")

    assert "jump-secret" not in str(error.value)
    assert jump_client.closed is True


def test_proxyjump_closes_the_jump_client_when_a_session_expires() -> None:
    connection = FakeConnection()
    jump_client = FakeJumpClient()
    clock = MutableClock()
    manager, _, _ = _proxy_manager(
        connection,
        jump_client,
        FakeAudit(),
        clock=clock,
    )
    info = manager.open_session("sw1")

    clock.value = 301
    with pytest.raises(SessionError, match="unknown or expired session"):
        manager.session_status(info.session_id)

    assert connection.disconnected is True
    assert jump_client.closed is True


def test_nested_connects_to_the_intermediate_host_and_reads_the_target() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager, host_keys, params = _nested_manager(connection, audit)

    info = manager.open_session("sw1")

    assert host_keys.calls == [("192.0.2.254", 22, "strict", False)]
    assert params[0]["transport"] == "ssh"
    assert params[0]["host"] == "192.0.2.254"
    assert params[0]["username"] == "term-operator"
    assert params[0]["password"] == "intermediate-secret"
    assert "sock" not in params[0]
    assert info.prompt == "switch#"
    assert audit.records[0]["route"] == "nested"
    assert any(w == "nested SSH uses the intermediate host's SSH client"
               for w in info.warnings)

    manager.close_session(info.session_id)
    assert connection.disconnected is True


def test_nested_login_sends_the_ssh_command_and_target_password() -> None:
    connection = FakeConnection()
    manager, _, _ = _nested_manager(connection, FakeAudit())

    manager.open_session("sw1")

    assert connection.writes == [
        "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null operator@192.0.2.1\n",
        "hunter2\n",
    ]
    assert connection.read_timeouts == [60.0]


def test_nested_telnet_login_sends_telnet_command_and_credentials() -> None:
    connection = FakeConnection()
    manager, _, _ = _nested_manager(connection, FakeAudit(), nested_telnet=True)

    info = manager.open_session("sw1")

    assert connection.writes == [
        "telnet 192.0.2.1\n",
        "operator\n",
        "hunter2\n",
    ]
    assert connection.read_timeouts == [60.0, 60.0]
    assert any(
        "nested Telnet transmits credentials and traffic in cleartext"
        for warning in info.warnings
    )


def test_direct_telnet_requires_allow_telnet_on_the_device() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), telnet=True, allow_telnet=False)

    with pytest.raises(TransportError, match="allow_telnet"):
        manager.open_session("sw1")
    assert connection.disconnected is False


def test_direct_telnet_requires_policy_allow() -> None:
    connections = ConnectionsConfig.model_validate(
        {
            "connections": {
                "direct-telnet": {"type": "direct", "protocol": "telnet"}
            }
        }
    )
    manager = SessionManager(
        AppConfig(
            inventory=InventoryConfig.model_validate(
                {
                    "devices": {
                        "sw1": {
                            "host": "192.0.2.1",
                            "credentials": "net",
                            "connection": "direct-telnet",
                            "allow_telnet": True,
                        }
                    }
                }
            ),
            connections=connections,
            credentials=CredentialsConfig.model_validate(
                {
                    "credentials": {
                        "net": {"backend": "pass", "entry": "network/net",
                                 "username": "operator"},
                    }
                }
            ),
            policy=PolicyConfig.model_validate({}),
            config_dir=Path("."),
        ),
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    with pytest.raises(TransportError, match="Telnet is denied"):
        manager.open_session("sw1")


def test_direct_telnet_uses_the_telnet_driver_without_host_key_check() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    host_keys = FakeHostKeys()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    manager = SessionManager(
        _config(telnet=True, allow_telnet=True),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=host_keys,  # type: ignore[arg-type]
    )

    info = manager.open_session("sw1")

    assert params[0]["transport"] == "telnet"
    assert params[0]["port"] == 23
    assert params[0]["password"] == "hunter2"
    assert "sock" not in params[0]
    assert host_keys.calls == []
    assert info.prompt == "switch#"
    assert any("Telnet transmits credentials and traffic in cleartext"
               for warning in info.warnings)
    assert audit.records[0]["route"] == "telnet"

    manager.close_session(info.session_id)
    assert connection.disconnected is True


def test_direct_telnet_uses_a_custom_port() -> None:
    connection = FakeConnection()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    connections = ConnectionsConfig.model_validate(
        {
            "connections": {
                "direct-telnet": {"type": "direct", "protocol": "telnet", "port": 2002}
            }
        }
    )
    manager = SessionManager(
        AppConfig(
            inventory=InventoryConfig.model_validate(
                {
                    "devices": {
                        "sw1": {
                            "host": "192.0.2.1",
                            "credentials": "net",
                            "connection": "direct-telnet",
                            "allow_telnet": True,
                        }
                    }
                }
            ),
            connections=connections,
            credentials=CredentialsConfig.model_validate(
                {
                    "credentials": {
                        "net": {"backend": "pass", "entry": "network/net",
                                 "username": "operator"},
                    }
                }
            ),
            policy=PolicyConfig.model_validate({"defaults": {"telnet": "allow"}}),
            config_dir=Path("."),
        ),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    manager.open_session("sw1")

    assert params[0]["port"] == 2002


def test_console_requires_allow_telnet_and_uses_the_telnet_driver() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    manager = SessionManager(
        _config(console=True, allow_telnet=True),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    info = manager.open_session("sw1")

    assert params[0]["transport"] == "telnet"
    assert params[0]["port"] == 2002
    assert "sock" not in params[0]
    assert any("console access transmits credentials and traffic in cleartext"
               for warning in info.warnings)
    assert audit.records[0]["route"] == "console"


def test_console_requires_allow_telnet_on_the_device() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), console=True, allow_telnet=False)

    with pytest.raises(TransportError, match="allow_telnet"):
        manager.open_session("sw1")


def test_legacy_direct_applies_disabled_algorithms_from_the_allowlist() -> None:
    connection = FakeConnection()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    connections = ConnectionsConfig.model_validate(
        {
            "connections": {
                "legacy": {
                    "type": "direct",
                    "protocol": "legacy_ssh",
                    "host_key_algorithms": ["ssh-rsa"],
                    "kex_algorithms": ["diffie-hellman-group1-sha1"],
                    "ciphers": ["aes128-cbc"],
                }
            }
        }
    )
    manager = SessionManager(
        AppConfig(
            inventory=InventoryConfig.model_validate(
                {
                    "devices": {
                        "sw1": {
                            "host": "192.0.2.1",
                            "credentials": "net",
                            "connection": "legacy",
                        }
                    }
                }
            ),
            connections=connections,
            credentials=CredentialsConfig.model_validate(
                {
                    "credentials": {
                        "net": {"backend": "pass", "entry": "network/net",
                                 "username": "operator"},
                    }
                }
            ),
            policy=PolicyConfig.model_validate({}),
            config_dir=Path("."),
        ),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    info = manager.open_session("sw1")

    disabled = params[0]["disabled_algorithms"]
    assert isinstance(disabled, dict)
    assert "ssh-rsa" not in disabled["keys"]
    assert "diffie-hellman-group1-sha1" not in disabled["kex"]
    assert "aes128-cbc" not in disabled["ciphers"]
    assert any("explicit legacy algorithm overrides in use" for warning in info.warnings)
    assert any("legacy SSH profile in use" for warning in info.warnings)
    connection = FakeConnection()
    host_keys = FakeHostKeys()
    manager, host_keys, _ = _nested_manager(
        connection, FakeAudit(), host_keys=host_keys
    )

    manager.open_session("sw1")

    assert host_keys.calls == [("192.0.2.254", 22, "strict", False)]


def test_nested_disconnects_the_connection_when_target_login_fails() -> None:
    connection = FakeConnection()
    connection.read_error = RuntimeError("intermediate login failed with intermediate-secret")
    manager, _, _ = _nested_manager(connection, FakeAudit())

    with pytest.raises(TransportError, match="intermediate login failed") as error:
        manager.open_session("sw1")

    assert "intermediate-secret" not in str(error.value)
    assert connection.disconnected is True


def test_nested_redacts_target_password_when_factory_fails() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager, _, _ = _nested_manager(
        connection,
        audit,
        connection_error=RuntimeError("target login failed with hunter2"),
    )

    with pytest.raises(TransportError, match="target login failed") as error:
        manager.open_session("sw1")

    assert "hunter2" not in str(error.value)
    assert connection.disconnected is False


def test_nested_session_expires_and_disconnects() -> None:
    connection = FakeConnection()
    clock = MutableClock()
    manager, _, _ = _nested_manager(
        connection, FakeAudit(), clock=clock
    )
    info = manager.open_session("sw1")

    clock.value = 301
    with pytest.raises(SessionError, match="unknown or expired session"):
        manager.session_status(info.session_id)

    assert connection.disconnected is True


def test_nested_telnet_requires_allow_telnet_on_the_device() -> None:
    connections = ConnectionsConfig.model_validate(
        {
            "connections": {
                "nested-term": {
                    "type": "nested",
                    "host": "192.0.2.254",
                    "protocol": "ssh",
                    "credentials": "intermediate",
                    "next_protocol": "telnet",
                }
            },
        }
    )
    manager = SessionManager(
        AppConfig(
            inventory=InventoryConfig.model_validate(
                {
                    "devices": {
                        "sw1": {
                            "host": "192.0.2.1",
                            "credentials": "net",
                            "connection": "nested-term",
                        }
                    }
                }
            ),
            connections=connections,
            credentials=CredentialsConfig.model_validate(
                {
                    "credentials": {
                        "net": {"backend": "pass", "entry": "network/net",
                                 "username": "operator"},
                        "intermediate": {"backend": "pass",
                                         "entry": "network/intermediate",
                                         "username": "term-operator"},
                    }
                }
            ),
            policy=PolicyConfig.model_validate({}),
            config_dir=Path("."),
        ),
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    with pytest.raises(TransportError, match="allow_telnet"):
        manager.open_session("sw1")


def test_nested_telnet_requires_policy_allow_even_with_allow_telnet() -> None:
    connections = ConnectionsConfig.model_validate(
        {
            "connections": {
                "nested-term": {
                    "type": "nested",
                    "host": "192.0.2.254",
                    "protocol": "ssh",
                    "credentials": "intermediate",
                    "next_protocol": "telnet",
                }
            },
        }
    )
    manager = SessionManager(
        AppConfig(
            inventory=InventoryConfig.model_validate(
                {
                    "devices": {
                        "sw1": {
                            "host": "192.0.2.1",
                            "credentials": "net",
                            "connection": "nested-term",
                            "allow_telnet": True,
                        }
                    }
                }
            ),
            connections=connections,
            credentials=CredentialsConfig.model_validate(
                {
                    "credentials": {
                        "net": {"backend": "pass", "entry": "network/net",
                                 "username": "operator"},
                        "intermediate": {"backend": "pass",
                                         "entry": "network/intermediate",
                                         "username": "term-operator"},
                    }
                }
            ),
            policy=PolicyConfig.model_validate({}),
            config_dir=Path("."),
        ),
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    with pytest.raises(TransportError, match="Telnet is denied"):
        manager.open_session("sw1")


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


def test_cli_help_clears_a_recognized_prompt_tail_without_waiting_for_output() -> None:
    connection = FakeConnection()
    connection.timing_output = "  version  Show software version\nswitch#show    \x08\x08\x08"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    result = manager.cli_help(info.session_id, "show ")

    assert result.output == "  version  Show software version\n"
    assert connection.read_timeouts == []
    assert connection.writes == ["show ?", "\x03", "\x15"]
    assert manager.session_status(info.session_id).state is SessionState.READY


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


def test_cli_help_cancels_input_after_a_read_timeout() -> None:
    connection = FakeConnection()
    connection.timing_error = RuntimeError("help read timed out")
    manager = _manager(connection, FakeAudit(), cli_help_timeout=3)
    info = manager.open_session("sw1")

    with pytest.raises(TransportError, match="help read timed out"):
        manager.cli_help(info.session_id, "show ")

    assert connection.timing_timeouts == [3.0]
    assert connection.read_timeouts == [3.0]
    assert connection.writes == ["show ?", "\x03"]
    assert manager.session_status(info.session_id).state is SessionState.FAILED


def test_cli_help_detects_paged_help_and_stays_in_paging() -> None:
    connection = FakeConnection()
    connection.timing_output = "  show usage\n---(more 35%)---"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    result = manager.cli_help(info.session_id, "show ")

    assert result.executed is True
    assert result.pager_active is True
    assert result.output == "  show usage\n---(more 35%)---"
    assert connection.writes == ["show ?"]
    assert manager.session_status(info.session_id).state is SessionState.PAGING


def test_cli_help_pager_pages_through_to_ready_and_clears_residual_line() -> None:
    connection = FakeConnection()
    connection.timing_output = "page1\n--More--"
    connection.read_outputs = ["page2\n--More--", "page3\nswitch#show "]
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.cli_help(info.session_id, "show ")

    first = manager.send_control(info.session_id, "space")
    second = manager.send_control(info.session_id, "space")

    assert first.pager_active is True
    assert first.output == "page2\n--More--"
    assert second.pager_active is False
    assert second.output == "page3\n"
    assert connection.writes == ["show ?", " ", " ", "\x03", "\x15"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_cli_help_pager_q_abort_clears_residual_line() -> None:
    connection = FakeConnection()
    connection.timing_output = "page1\n--More--"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.cli_help(info.session_id, "show ")

    result = manager.send_control(info.session_id, "q")

    assert result.action == "q"
    assert connection.writes == ["show ?", "q", "\x03", "\x15"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_cli_help_pager_page_limit_aborts_with_q() -> None:
    connection = FakeConnection()
    connection.timing_output = "page1\n--More--"
    manager = _manager(connection, FakeAudit(), max_pager_pages=1)
    info = manager.open_session("sw1")
    manager.cli_help(info.session_id, "show ")

    result = manager.send_control(info.session_id, "space")

    assert result.action == "q"
    assert connection.writes == ["show ?", "q", "\x03", "\x15"]
    assert manager.session_status(info.session_id).state is SessionState.READY


def test_command_pager_q_abort_does_not_send_extra_ctrl_c() -> None:
    connection = FakeConnection()
    connection.command_outputs["show interfaces"] = "page1\n--More--"
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")
    manager.run_command(info.session_id, "show interfaces")

    result = manager.send_control(info.session_id, "q")

    assert result.action == "q"
    assert connection.writes == ["q"]
    assert manager.session_status(info.session_id).state is SessionState.READY


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


def test_pager_detects_junos_more_marker_without_a_page_number() -> None:
    connection = FakeConnection()
    connection.command_outputs["show interfaces"] = "lines\n---(more)---"
    connection.read_outputs = ["next\nswitch#"]
    manager = _manager(connection, FakeAudit())
    info = manager.open_session("sw1")

    first = manager.run_command(info.session_id, "show interfaces")

    assert first.pager_active is True
    assert first.output == "lines\n---(more)---"
    assert manager.session_status(info.session_id).state is SessionState.PAGING

    second = manager.send_control(info.session_id, "space")
    assert second.pager_active is False
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
