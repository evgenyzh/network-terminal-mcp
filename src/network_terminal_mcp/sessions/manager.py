"""Session manager for raw interactive SSH/Telnet/console/serial terminals."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypedDict

import paramiko

from network_terminal_mcp.audit import AuditLogger
from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import (
    CredentialSpec,
    LegacyAlgorithms,
    OpenSpec,
    ProxyJumpRoute,
    SocksRoute,
    validate_pass_entry,
)
from network_terminal_mcp.connections.plan import (
    ConnectionPlan,
    build_plan,
    route_label,
)
from network_terminal_mcp.credentials.pass_backend import Credentials, PassBackend
from network_terminal_mcp.errors import SessionError, TransportError
from network_terminal_mcp.host_keys import HostKeyStatus, HostKeyStore, probe_host_key_socket
from network_terminal_mcp.redaction import Redactor
from network_terminal_mcp.sessions.models import (
    OutputChunk,
    SessionInfo,
    SessionState,
    TerminalOutput,
    TerminalSecretResult,
    TerminalWriteResult,
)
from network_terminal_mcp.socks import socks5_connect
from network_terminal_mcp.terminal import (
    SerialTerminal,
    SshTerminal,
    TelnetTerminal,
    TerminalConnection,
)

_READ_QUIET_SECONDS = 0.5

# Full algorithm sets Paramiko can offer; per-call allowlists become the
# complement via ``disabled_algorithms`` so legacy overrides are never global.
_PARAMIKO_ALGORITHMS: dict[str, list[str]] = {
    "keys": sorted(paramiko.Transport._preferred_keys),  # type: ignore[attr-defined]
    "kex": sorted(paramiko.Transport._preferred_kex),  # type: ignore[attr-defined]
    "ciphers": sorted(paramiko.Transport._preferred_ciphers),  # type: ignore[attr-defined]
}


class CredentialResolver(Protocol):
    """Credential backend abstraction used by tests and the real server."""

    def resolve(self, spec: CredentialSpec) -> Credentials: ...

    def read_entry(self, entry: str) -> str: ...


ConnectionFactory = Callable[[dict[str, object]], TerminalConnection]
Clock = Callable[[], float]


class _OutputFields(TypedDict):
    output: str
    truncated: bool
    output_offset: int | None
    next_output_offset: int | None


def _connection_factory_default(params: dict[str, object]) -> TerminalConnection:
    """Build a raw terminal from a transport params dict."""
    transport = params["transport"]
    connect_params = {k: v for k, v in params.items() if k != "transport"}
    terminal: TerminalConnection
    if transport == "telnet":
        terminal = TelnetTerminal()
    elif transport == "serial":
        terminal = SerialTerminal()
    elif transport == "ssh":
        terminal = SshTerminal()
    else:
        raise TransportError(f"unsupported transport {transport!r}")
    terminal.connect(**connect_params)  # type: ignore[arg-type]
    return terminal


def _disabled_algorithms(legacy: LegacyAlgorithms) -> dict[str, list[str]] | None:
    """Convert per-call allowlists into Paramiko ``disabled_algorithms``.

    Only categories explicitly listed in the call are restricted; the
    remaining categories keep Paramiko's default algorithm set. Returns None
    when nothing is restricted.
    """
    allowlists: dict[str, list[str] | None] = {
        "keys": legacy.host_key_algorithms,
        "kex": legacy.kex_algorithms,
        "ciphers": legacy.ciphers,
    }
    disabled: dict[str, list[str]] = {}
    for category, allowlist in allowlists.items():
        if allowlist is None:
            continue
        full = _PARAMIKO_ALGORITHMS[category]
        disabled[category] = [name for name in full if name not in allowlist]
    return disabled or None


@dataclass
class _ManagedSession:
    session_id: str
    plan: ConnectionPlan
    connection: TerminalConnection
    jump_client: paramiko.SSHClient | None
    prompt: str
    warnings: list[str]
    redactor: Redactor
    created_at: datetime
    last_used_at: datetime
    created_monotonic: float
    last_used_monotonic: float
    state: SessionState = SessionState.READY
    output: str = ""
    output_start: int = 0
    output_truncated: bool = False
    read_cursor: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionManager:
    """Own raw persistent sessions and expose synchronous terminal operations."""

    def __init__(
        self,
        config: AppConfig,
        *,
        connection_factory: ConnectionFactory = _connection_factory_default,
        credential_resolver: CredentialResolver | None = None,
        audit_logger: AuditLogger | None = None,
        host_keys: HostKeyStore | None = None,
        jump_client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
        clock: Clock = time.monotonic,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory
        self._credentials = credential_resolver or PassBackend()
        self._audit = audit_logger or AuditLogger(config.policy.runtime.audit_file)
        self._host_keys = host_keys or HostKeyStore(config.policy.runtime.known_hosts_file)
        self._jump_client_factory = jump_client_factory
        self._clock = clock
        self._sessions: dict[str, _ManagedSession] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def open_session(self, spec: OpenSpec) -> SessionInfo:
        """Open a model-described raw terminal session."""
        self._cleanup_expired()
        plan = build_plan(spec)
        self._require_connection_allowed(plan)
        credentials: Credentials | None = None
        route_credentials: Credentials | None = None
        if plan.protocol != "serial":
            assert plan.credentials is not None
            credentials = self._resolve_credentials(plan.credentials)
            if isinstance(plan.route, ProxyJumpRoute):
                route_credentials = self._resolve_credentials(plan.route.credentials)
        passwords = [
            secret
            for source in (credentials, route_credentials)
            if source is not None
            for secret in (source.password, source.key_passphrase)
            if secret is not None
        ]
        redactor = Redactor(passwords)
        warnings = list(plan.warnings)
        route = self._route_details(plan)
        username = credentials.username if credentials is not None else "-"

        self._audit_event(
            "open_session",
            plan=plan,
            username=username,
            outcome="started",
            redactor=redactor,
            **route,
        )
        connection: TerminalConnection | None = None
        jump_client: paramiko.SSHClient | None = None
        sock: object | None = None
        try:
            timeout = float(self._config.policy.runtime.io_timeout)
            if isinstance(plan.route, SocksRoute):
                assert plan.port is not None
                socks_route = plan.route
                host_key = self._host_keys.ensure(
                    plan.host,
                    port=plan.port,
                    policy=plan.host_key_policy,
                    timeout=timeout,
                    probe=lambda host, probe_port, probe_timeout: probe_host_key_socket(
                        socks5_connect(
                            socks_route.host,
                            socks_route.port,
                            host,
                            probe_port,
                            probe_timeout,
                        ),
                        probe_timeout,
                    ),
                )
                sock = socks5_connect(
                    socks_route.host,
                    socks_route.port,
                    plan.host,
                    plan.port,
                    timeout,
                )
            elif isinstance(plan.route, ProxyJumpRoute):
                assert route_credentials is not None
                jump_route = plan.route
                jump_host_key = self._host_keys.ensure(
                    jump_route.host,
                    port=jump_route.port,
                    policy=jump_route.host_key_policy,
                    timeout=timeout,
                )
                warnings.extend(
                    f"jump host: {warning}" for warning in self._host_key_warnings(jump_host_key)
                )
                jump_client = self._open_jump_client(jump_route, route_credentials)
                assert plan.port is not None
                host_key = self._host_keys.ensure(
                    plan.host,
                    port=plan.port,
                    policy=plan.host_key_policy,
                    timeout=timeout,
                    probe=lambda host, probe_port, probe_timeout: self._probe_via_jump(
                        jump_client, host, probe_port, probe_timeout
                    ),
                )
                sock = self._open_jump_channel(jump_client, plan.host, plan.port)
            elif plan.protocol == "serial":
                connection = self._connection_factory(
                    self._serial_transport_params(plan)
                )
                host_key = None
            elif plan.protocol in ("telnet", "console"):
                assert credentials is not None
                connection = self._connection_factory(
                    self._telnet_transport_params(plan, credentials)
                )
                host_key = None
            else:
                assert plan.port is not None
                host_key = self._host_keys.ensure(
                    plan.host,
                    port=plan.port,
                    policy=plan.host_key_policy,
                    timeout=timeout,
                )
            if host_key is not None:
                warnings.extend(self._host_key_warnings(host_key))
            if connection is None:
                assert credentials is not None
                assert plan.port is not None
                connection = self._connection_factory(
                    self._ssh_transport_params(
                        plan.host, plan.port, credentials, sock=sock, legacy=plan.legacy
                    )
                )
            prompt = ""
            try:
                prompt = connection.find_prompt()
            except TransportError:
                warnings.append(
                    "device prompt not detected; inspect output with terminal_read"
                )
        except Exception as exc:
            try:
                if connection is not None:
                    connection.disconnect()
            finally:
                self._close_jump_client(jump_client)
            if isinstance(exc, paramiko.AuthenticationException):
                message = (
                    f"authentication failed for final target {username}@"
                    f"{plan.host}:{plan.port}; the client permission gate was "
                    "not the problem - check the target credential reference"
                )
            else:
                message = redactor.redact(str(exc))
            self._audit_event(
                "open_session",
                plan=plan,
                username=username,
                outcome="failed",
                redactor=redactor,
                error=message,
                **route,
            )
            if isinstance(exc, TransportError):
                raise
            if isinstance(exc, paramiko.AuthenticationException):
                raise TransportError(message) from exc
            raise TransportError(f"failed to connect to {plan.host}: {message}") from exc

        now = self._clock()
        wall_now = datetime.now(UTC)
        session = _ManagedSession(
            session_id=uuid.uuid4().hex,
            plan=plan,
            connection=connection,
            jump_client=jump_client,
            prompt=redactor.redact(prompt),
            warnings=warnings,
            redactor=redactor,
            created_at=wall_now,
            last_used_at=wall_now,
            created_monotonic=now,
            last_used_monotonic=now,
        )
        with self._lock:
            if len(self._sessions) >= self._config.policy.runtime.max_open_sessions:
                try:
                    connection.disconnect()
                finally:
                    self._close_jump_client(jump_client)
                raise SessionError("maximum number of open sessions reached")
            self._sessions[session.session_id] = session
        self._audit_event(
            "open_session",
            plan=plan,
            username=username,
            outcome="completed",
            redactor=redactor,
            session_id=session.session_id,
            prompt=session.prompt,
            warnings=warnings,
            **route,
        )
        return self._session_info(session)

    def close_session(self, session_id: str) -> SessionInfo:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionError(f"unknown or expired session {session_id}")
        with session.lock:
            session.state = SessionState.CLOSING
            self._audit_event(
                "close_session",
                plan=session.plan,
                outcome="started",
                redactor=session.redactor,
                session_id=session_id,
            )
            try:
                session.connection.disconnect()
            finally:
                self._close_jump_client(session.jump_client)
                session.jump_client = None
                session.state = SessionState.CLOSED
                self._touch(session)
            self._audit_event(
                "close_session",
                plan=session.plan,
                outcome="completed",
                redactor=session.redactor,
                session_id=session_id,
            )
            return self._session_info(session)

    def session_status(self, session_id: str) -> SessionInfo:
        """Return current metadata for an active session."""
        session = self._get_session(session_id)
        with session.lock:
            return self._session_info(session)

    # ------------------------------------------------------------------
    # Raw terminal I/O
    # ------------------------------------------------------------------

    def terminal_write(
        self, session_id: str, data: str, *, enter: bool = True
    ) -> TerminalWriteResult:
        """Write exact input to the terminal stream.

        The whole ``data`` string is recorded in the audit (redacted only for
        secrets the server already knows). Passwords and other secrets must be
        sent with :meth:`terminal_write_secret` instead.
        """
        session = self._get_session(session_id)
        with session.lock:
            self._require_ready(session)
            payload = data + (self._enter_sequence(session) if enter else "")
            encoded = payload.encode("utf-8")
            if len(encoded) > self._config.policy.runtime.max_write_bytes:
                raise SessionError(
                    f"input exceeds max_write_bytes "
                    f"({self._config.policy.runtime.max_write_bytes})"
                )
            self._audit_interaction(
                session,
                "terminal_write",
                "started",
                data=session.redactor.redact(data),
                enter=enter,
                bytes=len(encoded),
            )
            try:
                session.connection.write_channel(payload)
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_interaction(
                    session, "terminal_write", "failed", error=message
                )
                raise TransportError(
                    f"terminal write failed in session {session_id}: {message}"
                ) from exc
            self._touch(session)
            self._audit_interaction(
                session, "terminal_write", "completed", bytes=len(encoded)
            )
            return TerminalWriteResult(
                session_id=session_id,
                bytes_sent=len(encoded),
                state=session.state,
            )

    def terminal_write_secret(
        self, session_id: str, entry: str
    ) -> TerminalSecretResult:
        """Send a ``pass`` entry value at a live password/passphrase prompt.

        The secret value is resolved inside the server, written to the stream,
        and added to redaction. Only the entry name and byte count are
        audited; the value never appears in tool arguments, results, or audit.
        """
        session = self._get_session(session_id)
        with session.lock:
            self._require_ready(session)
            try:
                validate_pass_entry(entry)
            except ValueError as exc:
                raise SessionError(f"invalid pass entry: {exc}") from exc
            value = self._credentials.read_entry(entry)
            if not value:
                raise SessionError(f"pass entry {entry!r} resolved to an empty secret")
            payload = value + self._enter_sequence(session)
            encoded = payload.encode("utf-8")
            self._audit_interaction(
                session,
                "terminal_write_secret",
                "started",
                entry=entry,
                bytes=len(encoded),
            )
            session.redactor.add(value)
            try:
                session.connection.write_channel(payload)
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_interaction(
                    session, "terminal_write_secret", "failed", entry=entry, error=message
                )
                raise TransportError(
                    f"secret write failed in session {session_id}: {message}"
                ) from exc
            self._touch(session)
            self._audit_interaction(
                session, "terminal_write_secret", "completed", entry=entry, bytes=len(encoded)
            )
            return TerminalSecretResult(
                session_id=session_id,
                entry=entry,
                bytes_sent=len(encoded),
                state=session.state,
            )

    def terminal_read(
        self, session_id: str, *, timeout: float | None = None
    ) -> TerminalOutput:
        """Read output until the stream is quiet or ``timeout`` expires.

        No prompt shape is required: pager screens, password prompts, banners,
        and normal output are all returned verbatim. Only output that arrived
        since the previous read is returned; when it is too large to inline,
        ``next_output_offset`` points at the first unread character and
        :meth:`read_output` continues from there.
        """
        session = self._get_session(session_id)
        with session.lock:
            self._require_ready(session)
            read_timeout = float(
                timeout if timeout is not None else self._config.policy.runtime.io_timeout
            )
            if read_timeout <= 0:
                raise SessionError("read timeout must be positive")
            maximum = float(self._config.policy.runtime.max_read_timeout)
            if read_timeout > maximum:
                raise SessionError(f"read timeout exceeds max_read_timeout ({maximum:g})")
            self._audit_interaction(session, "terminal_read", "started", timeout=read_timeout)
            try:
                raw = session.connection.read_channel_timing(
                    last_read=_READ_QUIET_SECONDS,
                    read_timeout=read_timeout,
                )
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_interaction(session, "terminal_read", "failed", error=message)
                raise TransportError(
                    f"terminal read failed in session {session_id}: {message}"
                ) from exc
            output = session.redactor.redact(raw)
            offset, fresh = self._append_output(session, output)
            inline, truncated = _truncate_utf8(
                fresh, self._config.policy.runtime.max_inline_output_bytes
            )
            if truncated:
                # The model has seen only ``inline``; keep the cursor at the
                # first unread character so read_output() returns the tail.
                session.read_cursor = offset + len(inline)
                next_offset: int | None = session.read_cursor
            else:
                session.read_cursor = offset + len(fresh)
                next_offset = None
            self._touch(session)
            self._audit_interaction(
                session,
                "terminal_read",
                "completed",
                bytes=len(output.encode()),
            )
            return TerminalOutput(
                session_id=session_id,
                output=inline,
                truncated=truncated,
                output_offset=offset,
                next_output_offset=next_offset,
            )

    def read_output(
        self, session_id: str, *, offset: int | None = None, limit: int | None = None
    ) -> OutputChunk:
        """Read accumulated, redacted session output.

        Without ``offset`` this continues from the caller's read cursor and
        returns only output that has not been seen yet; repeated calls cannot
        duplicate history. An explicit ``offset`` is a deliberate random
        access into the bounded buffer (for example ``next_output_offset``
        from a truncated read) and does not move the cursor backwards.
        """
        if offset is not None and offset < 0:
            raise SessionError("output offset must be non-negative")
        if limit is None:
            limit = self._config.policy.runtime.max_inline_output_bytes
        if limit <= 0:
            raise SessionError("output limit must be positive")
        session = self._get_session(session_id)
        with session.lock:
            start = session.read_cursor if offset is None else offset
            if start < session.output_start:
                message = (
                    "output before offset "
                    f"{session.output_start} has expired from the session buffer"
                )
                self._audit_interaction(
                    session, "read_output", "failed", offset=start, error=message
                )
                raise SessionError(message)
            relative_offset = start - session.output_start
            chunk = session.output[relative_offset : relative_offset + limit]
            next_offset = start + len(chunk)
            buffer_end = session.output_start + len(session.output)
            if start <= session.read_cursor:
                session.read_cursor = max(session.read_cursor, next_offset)
            self._touch(session)
            self._audit_interaction(
                session,
                "read_output",
                "completed",
                offset=start,
                bytes=len(chunk),
                next_offset=next_offset if next_offset < buffer_end else None,
                cursor=session.read_cursor,
            )
            return OutputChunk(
                session_id=session_id,
                offset=start,
                output=chunk,
                next_offset=next_offset if next_offset < buffer_end else None,
                oldest_offset=session.output_start,
                truncated=session.output_truncated,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _enter_sequence(session: _ManagedSession) -> str:
        """Return the line terminator for the session's transport."""
        return "\r" if session.plan.protocol == "serial" else "\n"

    def _resolve_credentials(self, spec: CredentialSpec) -> Credentials:
        return self._credentials.resolve(spec)

    def _require_connection_allowed(self, plan: ConnectionPlan) -> None:
        """Apply local hard-deny policy gates to insecure connection options."""
        defaults = self._config.policy.defaults
        if plan.protocol in ("telnet", "console"):
            if not plan.allow_telnet:
                raise TransportError(
                    f"{plan.protocol} requires allow_telnet=true on the call"
                )
            if not defaults.allow_telnet:
                raise TransportError("Telnet is disabled by policy defaults")
        if plan.protocol == "serial":
            if not plan.allow_serial:
                raise TransportError("serial requires allow_serial=true on the call")
            if not defaults.allow_serial:
                raise TransportError("serial is disabled by policy defaults")
        if plan.credentials is not None and plan.credentials.backend == "plaintext":
            if not plan.allow_plaintext_password:
                raise TransportError(
                    "plaintext credentials require allow_plaintext_password=true"
                )
            if not defaults.allow_plaintext_password:
                raise TransportError(
                    "plaintext credentials are disabled by policy defaults"
                )
        if plan.legacy is not None and not defaults.allow_legacy_algorithms:
            raise TransportError(
                "legacy SSH algorithms are disabled by policy defaults"
            )

    def _open_jump_client(
        self, route: ProxyJumpRoute, credentials: Credentials
    ) -> paramiko.SSHClient:
        """Authenticate the jump host after its key has been checked."""
        timeout = float(self._config.policy.runtime.io_timeout)
        client = self._jump_client_factory()
        try:
            client.load_host_keys(str(self._host_keys.path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            if credentials.key_file is not None:
                client.connect(
                    route.host,
                    port=route.port,
                    username=credentials.username,
                    key_filename=credentials.key_file,
                    passphrase=credentials.key_passphrase,
                    timeout=timeout,
                    banner_timeout=timeout,
                    auth_timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            elif credentials.password is not None:
                client.connect(
                    route.host,
                    port=route.port,
                    username=credentials.username,
                    password=credentials.password,
                    timeout=timeout,
                    banner_timeout=timeout,
                    auth_timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            else:
                raise TransportError("jump credential has no password or SSH key")
        except paramiko.AuthenticationException as exc:
            client.close()
            raise TransportError(
                "jump host authentication failed for "
                f"{credentials.username}@{route.host}:{route.port}; the client "
                "permission gate was not the problem - check the jump credential "
                "reference (key file or pass entry)"
            ) from exc
        except Exception:
            client.close()
            raise
        return client

    def _probe_via_jump(
        self, jump_client: paramiko.SSHClient, host: str, port: int, timeout: float
    ) -> paramiko.PKey:
        """Read the final target key through a fresh forwarded channel."""
        return probe_host_key_socket(self._open_jump_channel(jump_client, host, port), timeout)

    def _open_jump_channel(
        self, jump_client: paramiko.SSHClient, host: str, port: int
    ) -> object:
        transport = jump_client.get_transport()
        if transport is None or not transport.is_active():
            raise TransportError("SSH jump host transport is not active")
        channel = transport.open_channel(
            "direct-tcpip",
            (host, port),
            ("127.0.0.1", 0),
            timeout=float(self._config.policy.runtime.io_timeout),
        )
        if channel is None:
            raise TransportError("SSH jump host refused the target forwarding channel")
        return channel

    @staticmethod
    def _close_jump_client(jump_client: paramiko.SSHClient | None) -> None:
        if jump_client is not None:
            jump_client.close()

    def _ssh_transport_params(
        self,
        host: str,
        port: int,
        credentials: Credentials,
        *,
        sock: object | None = None,
        legacy: LegacyAlgorithms | None = None,
    ) -> dict[str, object]:
        timeout = float(self._config.policy.runtime.io_timeout)
        params: dict[str, object] = {
            "transport": "ssh",
            "host": host,
            "port": port,
            "username": credentials.username,
            "known_hosts_file": str(self._host_keys.path),
            "conn_timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
        }
        if legacy is not None:
            disabled = _disabled_algorithms(legacy)
            if disabled is not None:
                params["disabled_algorithms"] = disabled
        if credentials.key_file is not None:
            params["key_file"] = credentials.key_file
            if credentials.key_passphrase is not None:
                params["passphrase"] = credentials.key_passphrase
        elif credentials.password is not None:
            params["password"] = credentials.password
        else:
            raise TransportError("target credential has no password or SSH key")
        if sock is not None:
            params["sock"] = sock
        return params

    def _telnet_transport_params(
        self, plan: ConnectionPlan, credentials: Credentials
    ) -> dict[str, object]:
        if credentials.password is None:
            raise TransportError("Telnet target credential has no password")
        assert plan.port is not None
        timeout = float(self._config.policy.runtime.io_timeout)
        return {
            "transport": "telnet",
            "host": plan.host,
            "port": plan.port,
            "username": credentials.username,
            "password": credentials.password,
            "conn_timeout": timeout,
        }

    def _serial_transport_params(self, plan: ConnectionPlan) -> dict[str, object]:
        timeout = float(self._config.policy.runtime.io_timeout)
        return {
            "transport": "serial",
            "device": plan.host,
            "baudrate": plan.baudrate,
            "bytesize": plan.bytesize,
            "parity": plan.parity,
            "stopbits": plan.stopbits,
            "conn_timeout": timeout,
        }

    @staticmethod
    def _route_details(plan: ConnectionPlan) -> dict[str, object]:
        details: dict[str, object] = {
            "route": route_label(plan),
            "protocol": plan.protocol,
        }
        if isinstance(plan.route, SocksRoute):
            details["proxy_host"] = plan.route.host
            details["proxy_port"] = plan.route.port
        elif isinstance(plan.route, ProxyJumpRoute):
            details["jump_host"] = plan.route.host
            details["jump_port"] = plan.route.port
        return details

    @staticmethod
    def _host_key_warnings(status: HostKeyStatus) -> list[str]:
        if status.enrolled:
            if status.changed:
                return [
                    "host key CHANGED and auto-accepted under accept_changed policy: "
                    f"{status.previous_fingerprint} -> {status.fingerprint}; "
                    "verify out of band if this is unexpected"
                ]
            return [
                "host key enrolled with TOFU: "
                f"{status.algorithm} {status.fingerprint}; "
                "use host_key_policy strict afterwards"
            ]
        return []

    def _get_session(self, session_id: str) -> _ManagedSession:
        self._cleanup_expired()
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"unknown or expired session {session_id}")
        return session

    @staticmethod
    def _require_ready(session: _ManagedSession) -> None:
        if session.state != SessionState.READY:
            raise SessionError(
                f"session {session.session_id} is not ready: {session.state.value}"
            )

    @staticmethod
    def _fail_session(session: _ManagedSession) -> None:
        session.state = SessionState.FAILED

    def _cleanup_expired(self) -> None:
        now = self._clock()
        expired: list[_ManagedSession] = []
        with self._lock:
            for session_id, session in tuple(self._sessions.items()):
                idle = now - session.last_used_monotonic
                lifetime = now - session.created_monotonic
                if (
                    idle > self._config.policy.runtime.session_idle_timeout
                    or lifetime > self._config.policy.runtime.session_max_lifetime
                ):
                    self._sessions.pop(session_id)
                    expired.append(session)
        for session in expired:
            with session.lock:
                session.state = SessionState.CLOSED
                try:
                    session.connection.disconnect()
                finally:
                    self._close_jump_client(session.jump_client)
                    session.jump_client = None
                    self._touch(session)
                self._audit_event(
                    "close_session",
                    plan=session.plan,
                    outcome="expired",
                    redactor=session.redactor,
                    session_id=session.session_id,
                )

    def _append_output(self, session: _ManagedSession, output: str) -> tuple[int, str]:
        """Append redacted output and return (fresh offset, kept fresh text).

        When the bounded buffer drops leading characters, the returned offset
        is clamped to the new buffer start so callers never point at evicted
        data, and the read cursor is pulled forward with the buffer.
        """
        previous_end = session.output_start + len(session.output)
        combined = session.output + output
        trimmed, dropped = _keep_utf8_tail(
            combined, self._config.policy.runtime.max_session_buffer_bytes
        )
        if dropped:
            session.output_start += dropped
            session.output_truncated = True
        session.output = trimmed
        start = max(previous_end, session.output_start)
        session.read_cursor = max(session.read_cursor, session.output_start)
        return start, session.output[start - session.output_start :]

    def _touch(self, session: _ManagedSession) -> None:
        session.last_used_monotonic = self._clock()
        session.last_used_at = datetime.now(UTC)

    def _session_info(self, session: _ManagedSession) -> SessionInfo:
        return SessionInfo(
            session_id=session.session_id,
            route=route_label(session.plan),
            host=session.plan.host,
            prompt=session.prompt,
            state=session.state,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            warnings=list(session.warnings),
        )

    def _audit_interaction(
        self,
        session: _ManagedSession,
        event: str,
        outcome: str,
        **details: object,
    ) -> None:
        self._audit_event(
            event,
            plan=session.plan,
            outcome=outcome,
            redactor=session.redactor,
            session_id=session.session_id,
            **details,
        )

    def _audit_event(
        self,
        event: str,
        *,
        plan: ConnectionPlan,
        outcome: str,
        redactor: Redactor,
        **details: object,
    ) -> None:
        record: dict[str, object] = {
            "event": event,
            "outcome": outcome,
            "host": plan.host,
            "route": route_label(plan),
            "protocol": plan.protocol,
        }
        record.update(details)
        self._audit.write(_redact_record(record, redactor))


def _redact_record(record: dict[str, object], redactor: Redactor) -> dict[str, object]:
    return {
        key: redactor.redact(value) if isinstance(value, str) else value
        for key, value in record.items()
    }


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode(errors="ignore"), True


def _keep_utf8_tail(text: str, max_bytes: int) -> tuple[str, int]:
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text, 0
    kept = encoded[-max_bytes:].decode(errors="ignore")
    return kept, len(text) - len(kept)
