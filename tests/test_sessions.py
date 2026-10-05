"""Unit tests for raw terminal session management."""

from __future__ import annotations

import json
from pathlib import Path

import paramiko
import pytest
from pydantic import ValidationError

from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import (
    CredentialSpec,
    OpenSpec,
    PolicyConfig,
)
from network_terminal_mcp.credentials.pass_backend import Credentials
from network_terminal_mcp.errors import SessionError, TransportError
from network_terminal_mcp.host_keys import HostKeyStatus
from network_terminal_mcp.sessions import SessionInfo, SessionManager, SessionState
from network_terminal_mcp.terminal import TerminalConnection


class FakeConnection:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.timing_output = ""
        self.timing_error: Exception | None = None
        self.write_error: Exception | None = None
        self.timing_timeouts: list[float] = []
        self.prompt = "switch#"
        self.prompt_error: Exception | None = None
        self.disconnected = False

    def find_prompt(self) -> str:
        if self.prompt_error is not None:
            raise self.prompt_error
        return self.prompt

    def write_channel(self, out_data: str) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(out_data)

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
    def __init__(self) -> None:
        self.resolved: list[CredentialSpec] = []
        self.read_entries: list[str] = []
        self.entries: dict[str, str] = {"network/secret": "typed-secret"}

    def resolve(self, spec: CredentialSpec) -> Credentials:
        self.resolved.append(spec)
        if spec.backend == "ssh_key":
            return Credentials(
                username=spec.username,
                key_file=f"/resolved/{spec.key_file}",
            )
        if spec.backend == "plaintext":
            assert spec.password is not None
            return Credentials(
                username=spec.username, password=spec.password.get_secret_value()
            )
        if spec.username == "jump-operator":
            password = "jump-secret"
        else:
            password = "hunter2"
        return Credentials(username=spec.username, password=password)

    def read_entry(self, entry: str) -> str:
        self.read_entries.append(entry)
        return self.entries.get(entry, "")


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


def _policy(
    *,
    allow_telnet: bool = True,
    allow_plaintext_password: bool = True,
    allow_legacy_algorithms: bool = True,
    allow_serial: bool = True,
    runtime: dict[str, object] | None = None,
) -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "defaults": {
                "allow_telnet": allow_telnet,
                "allow_plaintext_password": allow_plaintext_password,
                "allow_legacy_algorithms": allow_legacy_algorithms,
                "allow_serial": allow_serial,
            },
            "runtime": runtime or {},
        }
    )


def _config(*, policy: PolicyConfig | None = None) -> AppConfig:
    return AppConfig(policy=policy or _policy(), config_dir=Path("."))


def _credentials(
    username: str = "operator", entry: str = "network/net"
) -> dict[str, object]:
    return {"backend": "pass", "entry": entry, "username": username}


def _spec(**overrides: object) -> OpenSpec:
    data: dict[str, object] = {"host": "192.0.2.1", "credentials": _credentials()}
    data.update(overrides)
    return OpenSpec.model_validate(data)


def _open(manager: SessionManager, **overrides: object) -> SessionInfo:
    return manager.open_session(_spec(**overrides))


def _jump_route(*, key: bool = False) -> dict[str, object]:
    credentials: dict[str, object] = (
        {
            "backend": "ssh_key",
            "key_file": "/tmp/jump-key",
            "username": "jump-operator",
        }
        if key
        else _credentials("jump-operator", "network/jump")
    )
    return {"type": "proxyjump", "host": "192.0.2.254", "credentials": credentials}


def _socks_route() -> dict[str, object]:
    return {"type": "socks", "host": "127.0.0.1", "port": 10900}


def _manager(
    connection: TerminalConnection,
    audit: FakeAudit,
    clock: MutableClock | None = None,
    *,
    policy: PolicyConfig | None = None,
    credentials: FakeCredentials | None = None,
    **runtime: object,
) -> SessionManager:
    return SessionManager(
        _config(policy=policy or _policy(runtime=runtime)),
        connection_factory=lambda params: connection,
        credential_resolver=credentials or FakeCredentials(),
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
) -> tuple[SessionManager, FakeHostKeys, list[dict[str, object]]]:
    resolved_host_keys = host_keys or FakeHostKeys()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        if connection_error is not None:
            raise connection_error
        return connection

    manager = SessionManager(
        _config(),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=resolved_host_keys,  # type: ignore[arg-type]
        jump_client_factory=lambda: jump_client,  # type: ignore[arg-type,return-value]
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
        _config(),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=resolved_host_keys,  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )
    return manager, resolved_host_keys, params


# ---------------------------------------------------------------------------
# open_session
# ---------------------------------------------------------------------------


def test_open_session_uses_factory_and_reports_route() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = _open(manager)
    assert info.state is SessionState.READY
    assert info.prompt == "switch#"
    assert info.route == "direct"
    assert info.host == "192.0.2.1"


def test_direct_ssh_transport_params() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    manager = SessionManager(
        _config(),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=audit,  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )
    _open(manager)

    assert params[0]["transport"] == "ssh"
    assert params[0]["host"] == "192.0.2.1"
    assert params[0]["port"] == 22
    assert params[0]["username"] == "operator"
    assert params[0]["password"] == "hunter2"
    assert params[0]["known_hosts_file"] == "/tmp/known_hosts"
    assert "sock" not in params[0]
    assert audit.records[0]["route"] == "direct"
    assert audit.records[0]["username"] == "operator"


def test_prompt_failure_keeps_the_session_usable() -> None:
    connection = FakeConnection()
    connection.prompt_error = TransportError("unable to find the device prompt")
    manager = _manager(connection, FakeAudit())

    info = _open(manager)

    assert info.state is SessionState.READY
    assert info.prompt == ""
    assert any("prompt not detected" in warning for warning in info.warnings)


def test_legacy_algorithms_become_disabled_algorithms() -> None:
    connection = FakeConnection()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    manager = SessionManager(
        _config(),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )
    _open(manager, legacy={"ciphers": ["aes128-cbc"]})

    disabled = params[0]["disabled_algorithms"]
    assert isinstance(disabled, dict)
    assert "aes128-cbc" not in disabled["ciphers"]


def test_proxyjump_uses_a_forwarded_socket_and_closes_the_jump_client() -> None:
    connection = FakeConnection()
    jump_client = FakeJumpClient()
    audit = FakeAudit()
    manager, host_keys, params = _proxy_manager(connection, jump_client, audit)

    info = _open(manager, route=_jump_route())

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


def test_socks_route_reaches_target_through_a_socks_socket(
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

    info = _open(manager, route=_socks_route(), host_key_policy="accept_new")

    assert host_keys.calls == [("192.0.2.1", 22, "accept_new", True)]
    assert socks_targets == [("192.0.2.1", 22, 60.0)]
    assert params[0]["host"] == "192.0.2.1"
    assert params[0]["sock"] is fake_socket
    assert audit.records[0]["route"] == "socks"

    manager.close_session(info.session_id)
    assert connection.disconnected is True


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

    info = _open(manager, route=_jump_route())

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
        _open(manager, route=_jump_route())

    assert "hunter2" not in str(error.value)
    assert "jump-secret" not in str(error.value)
    assert jump_client.closed is True


def test_proxyjump_closes_the_jump_client_when_bastion_login_fails() -> None:
    jump_client = FakeJumpClient()
    jump_client.connect_error = RuntimeError("bastion login failed with jump-secret")
    manager, _, _ = _proxy_manager(FakeConnection(), jump_client, FakeAudit())

    with pytest.raises(TransportError, match="bastion login failed") as error:
        _open(manager, route=_jump_route())

    assert "jump-secret" not in str(error.value)
    assert jump_client.closed is True


def test_proxyjump_reports_jump_authentication_failure_clearly() -> None:
    jump_client = FakeJumpClient()
    jump_client.connect_error = paramiko.AuthenticationException("Authentication failed.")
    audit = FakeAudit()
    manager, _, _ = _proxy_manager(FakeConnection(), jump_client, audit)

    with pytest.raises(TransportError, match="jump host authentication failed") as error:
        _open(manager, route=_jump_route())

    assert "jump-operator@192.0.2.254:22" in str(error.value)
    assert "permission gate was not the problem" in str(error.value)
    assert jump_client.closed is True
    assert "jump host authentication failed" in str(audit.records[-1]["error"])


def test_proxyjump_reports_final_target_authentication_failure_clearly() -> None:
    jump_client = FakeJumpClient()
    manager, _, _ = _proxy_manager(
        FakeConnection(),
        jump_client,
        FakeAudit(),
        connection_error=paramiko.AuthenticationException("Authentication failed."),
    )

    with pytest.raises(
        TransportError, match="authentication failed for final target"
    ) as error:
        _open(manager, route=_jump_route())

    assert "operator@192.0.2.1:22" in str(error.value)
    assert "permission gate was not the problem" in str(error.value)


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
    info = _open(manager, route=_jump_route())

    clock.value = 100_000
    with pytest.raises(SessionError, match="unknown or expired session"):
        manager.session_status(info.session_id)

    assert connection.disconnected is True
    assert jump_client.closed is True


def test_console_requires_an_explicit_port_at_spec_validation() -> None:
    with pytest.raises(ValidationError, match="console protocol requires"):
        _spec(protocol="console", allow_telnet=True)


def test_telnet_policy_hard_deny() -> None:
    connection = FakeConnection()
    manager = _manager(
        connection, FakeAudit(), policy=_policy(allow_telnet=False)
    )

    with pytest.raises(TransportError, match="Telnet is disabled by policy defaults"):
        _open(manager, protocol="telnet", allow_telnet=True)
    assert connection.disconnected is False


def test_direct_telnet_uses_the_telnet_driver_without_host_key_check() -> None:
    connection = FakeConnection()
    host_keys = FakeHostKeys()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    manager = SessionManager(
        _config(),
        connection_factory=factory,
        credential_resolver=FakeCredentials(),
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=host_keys,  # type: ignore[arg-type]
    )

    info = _open(manager, protocol="telnet", allow_telnet=True, port=2323)

    assert host_keys.calls == []
    assert params[0]["transport"] == "telnet"
    assert params[0]["port"] == 2323
    assert info.route == "telnet"


# ---------------------------------------------------------------------------
# serial
# ---------------------------------------------------------------------------


def test_serial_plan_and_transport_params() -> None:
    connection = FakeConnection()
    credentials = FakeCredentials()
    params: list[dict[str, object]] = []

    def factory(connection_params: dict[str, object]) -> FakeConnection:
        params.append(connection_params)
        return connection

    manager = SessionManager(
        _config(),
        connection_factory=factory,
        credential_resolver=credentials,
        audit_logger=FakeAudit(),  # type: ignore[arg-type]
        host_keys=FakeHostKeys(),  # type: ignore[arg-type]
    )

    info = manager.open_session(
        _spec(
            protocol="serial",
            host="/dev/ttyUSB0",
            credentials=None,
            allow_serial=True,
            serial={"baudrate": 115200, "parity": "E", "stopbits": 1.5},
        )
    )

    assert credentials.resolved == []
    assert params[0] == {
        "transport": "serial",
        "device": "/dev/ttyUSB0",
        "baudrate": 115200,
        "bytesize": 8,
        "parity": "E",
        "stopbits": 1.5,
        "conn_timeout": 60.0,
    }
    assert info.route == "serial"
    assert any("local serial console" in warning for warning in info.warnings)


def test_serial_requires_the_allow_flag_at_spec_validation() -> None:
    with pytest.raises(ValidationError, match="allow_serial"):
        _spec(protocol="serial", host="/dev/ttyUSB0", credentials=None)


def test_serial_rejects_non_dev_paths_and_ports() -> None:
    with pytest.raises(ValidationError, match="absolute /dev/"):
        _spec(
            protocol="serial",
            host="/tmp/ttyUSB0",
            credentials=None,
            allow_serial=True,
        )
    with pytest.raises(ValidationError, match="does not use a TCP port"):
        _spec(
            protocol="serial",
            host="/dev/ttyUSB0",
            credentials=None,
            allow_serial=True,
            port=22,
        )


def test_serial_policy_hard_deny() -> None:
    connection = FakeConnection()
    manager = _manager(
        connection, FakeAudit(), policy=_policy(allow_serial=False)
    )

    with pytest.raises(TransportError, match="serial is disabled by policy defaults"):
        _open(
            manager,
            protocol="serial",
            host="/dev/ttyUSB0",
            credentials=None,
            allow_serial=True,
        )
    assert connection.disconnected is False


# ---------------------------------------------------------------------------
# terminal_write
# ---------------------------------------------------------------------------


def test_terminal_write_appends_enter_by_default() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = _open(manager)

    result = manager.terminal_write(info.session_id, "display version")

    assert connection.writes == ["display version\n"]
    assert result.bytes_sent == len("display version\n")
    write_records = [r for r in audit.records if r["event"] == "terminal_write"]
    assert write_records[0]["data"] == "display version"
    assert write_records[0]["enter"] is True


def test_terminal_write_without_enter_sends_control_bytes() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = _open(manager)

    manager.terminal_write(info.session_id, "\x03", enter=False)
    manager.terminal_write(info.session_id, " ", enter=False)

    assert connection.writes == ["\x03", " "]


def test_terminal_write_serial_uses_carriage_return() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = manager.open_session(
        _spec(protocol="serial", host="/dev/ttyUSB0", credentials=None, allow_serial=True)
    )

    manager.terminal_write(info.session_id, "show version")

    assert connection.writes == ["show version\r"]


def test_terminal_write_rejects_oversized_input() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), max_write_bytes=5)
    info = _open(manager)

    with pytest.raises(SessionError, match="max_write_bytes"):
        manager.terminal_write(info.session_id, "show version")
    assert connection.writes == []


def test_terminal_write_redacts_known_secrets_in_result_and_audit() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = _open(manager)

    result = manager.terminal_write(info.session_id, "password hunter2", enter=False)

    assert "hunter2" not in result.data
    write_records = [r for r in audit.records if r["event"] == "terminal_write"]
    assert "hunter2" not in json.dumps(write_records)


def test_transport_failure_marks_the_session_failed() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    manager = _manager(connection, audit)
    info = _open(manager)
    connection.write_error = OSError("broken pipe with hunter2")

    with pytest.raises(TransportError, match="terminal write failed") as error:
        manager.terminal_write(info.session_id, "show version")

    assert "hunter2" not in str(error.value)
    assert manager.session_status(info.session_id).state is SessionState.FAILED
    with pytest.raises(SessionError, match="not ready"):
        manager.terminal_write(info.session_id, "show version")


# ---------------------------------------------------------------------------
# terminal_read
# ---------------------------------------------------------------------------


def test_terminal_read_returns_output_and_buffers_it() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = _open(manager)
    connection.timing_output = "hello\nswitch#"

    result = manager.terminal_read(info.session_id, timeout=2.0)

    assert result.output == "hello\nswitch#"
    assert result.output_offset == 0
    assert result.truncated is False
    chunk = manager.read_output(info.session_id, offset=0)
    assert chunk.output == "hello\nswitch#"


def test_terminal_read_truncates_large_inline_output() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), max_inline_output_bytes=8)
    info = _open(manager)
    connection.timing_output = "0123456789abcdef"

    result = manager.terminal_read(info.session_id, timeout=2.0)

    assert result.truncated is True
    assert result.output == "01234567"
    assert result.next_output_offset == 16
    rest = manager.read_output(info.session_id, offset=8)
    assert rest.output == "89abcdef"


def test_terminal_read_validates_timeout_bounds() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), max_read_timeout=10)
    info = _open(manager)

    with pytest.raises(SessionError, match="positive"):
        manager.terminal_read(info.session_id, timeout=0)
    with pytest.raises(SessionError, match="max_read_timeout"):
        manager.terminal_read(info.session_id, timeout=11)


def test_terminal_read_failure_marks_the_session_failed() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = _open(manager)
    connection.timing_error = OSError("read failed")

    with pytest.raises(TransportError, match="terminal read failed"):
        manager.terminal_read(info.session_id)

    assert manager.session_status(info.session_id).state is SessionState.FAILED


# ---------------------------------------------------------------------------
# terminal_write_secret
# ---------------------------------------------------------------------------


def test_terminal_write_secret_resolves_redacts_and_audits_entry_only() -> None:
    connection = FakeConnection()
    audit = FakeAudit()
    credentials = FakeCredentials()
    manager = _manager(connection, audit, credentials=credentials)
    info = _open(manager)

    result = manager.terminal_write_secret(info.session_id, "network/secret")

    assert connection.writes == ["typed-secret\n"]
    assert credentials.read_entries == ["network/secret"]
    assert result.entry == "network/secret"
    assert result.bytes_sent == len("typed-secret\n")
    records = json.dumps(audit.records)
    assert "typed-secret" not in records
    assert "network/secret" in records

    connection.timing_output = "got typed-secret"
    read = manager.terminal_read(info.session_id)
    assert read.output == "got <redacted>"


def test_terminal_write_secret_rejects_invalid_and_empty_entries() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = _open(manager)

    with pytest.raises(SessionError, match="pass entry"):
        manager.terminal_write_secret(info.session_id, "../secret")
    with pytest.raises(SessionError, match="empty secret"):
        manager.terminal_write_secret(info.session_id, "network/missing")
    assert connection.writes == []


# ---------------------------------------------------------------------------
# session bookkeeping
# ---------------------------------------------------------------------------


def test_read_output_rejects_expired_offsets() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), max_session_buffer_bytes=8)
    info = _open(manager)
    connection.timing_output = "abcdefghij"

    manager.terminal_read(info.session_id, timeout=1.0)

    with pytest.raises(SessionError, match="expired"):
        manager.read_output(info.session_id, offset=0)
    chunk = manager.read_output(info.session_id, offset=2)
    assert chunk.output == "cdefghij"
    assert chunk.truncated is True


def test_session_expires_after_idle_timeout() -> None:
    connection = FakeConnection()
    clock = MutableClock()
    manager = _manager(
        connection, FakeAudit(), clock=clock, session_idle_timeout=10
    )
    info = _open(manager)

    clock.value = 11
    with pytest.raises(SessionError, match="unknown or expired session"):
        manager.session_status(info.session_id)
    assert connection.disconnected is True


def test_max_open_sessions_is_enforced() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit(), max_open_sessions=1)
    _open(manager)

    with pytest.raises(SessionError, match="maximum number of open sessions"):
        _open(manager)


def test_close_session_disconnects_and_reports_closed() -> None:
    connection = FakeConnection()
    manager = _manager(connection, FakeAudit())
    info = _open(manager)

    closed = manager.close_session(info.session_id)

    assert closed.state is SessionState.CLOSED
    assert connection.disconnected is True
    with pytest.raises(SessionError, match="unknown or expired session"):
        manager.session_status(info.session_id)
