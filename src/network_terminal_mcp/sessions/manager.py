"""Direct SSH session manager backed by Netmiko."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast

from netmiko import ConnectHandler

from network_terminal_mcp.audit import AuditLogger
from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import Action, CredentialProfile, DirectConnection
from network_terminal_mcp.credentials.pass_backend import Credentials, PassBackend
from network_terminal_mcp.errors import PolicyError, SessionError, TransportError
from network_terminal_mcp.host_keys import HostKeyStatus, HostKeyStore
from network_terminal_mcp.platforms import Platform, PlatformRegistry
from network_terminal_mcp.policy.engine import PolicyEngine
from network_terminal_mcp.redaction import Redactor
from network_terminal_mcp.sessions.models import (
    CommandResult,
    OutputChunk,
    SessionInfo,
    SessionState,
)
from network_terminal_mcp.targets.resolver import Target, TargetResolver


class TerminalConnection(Protocol):
    """Subset of Netmiko's connection API required by the manager."""

    def find_prompt(self) -> str: ...

    def send_command(self, command_string: str, *, read_timeout: float) -> str: ...

    def disconnect(self) -> None: ...


class CredentialResolver(Protocol):
    """Credential backend abstraction used by tests and the real server."""

    def resolve(self, profile: CredentialProfile) -> Credentials: ...


ConnectionFactory = Callable[[dict[str, object]], TerminalConnection]
Clock = Callable[[], float]


def _netmiko_connection_factory(params: dict[str, object]) -> TerminalConnection:
    return cast(TerminalConnection, ConnectHandler(**params))


@dataclass
class _ManagedSession:
    session_id: str
    target: Target
    platform: Platform
    connection: TerminalConnection
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
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionManager:
    """Own direct SSH sessions and expose safe, synchronous operations."""

    def __init__(
        self,
        config: AppConfig,
        *,
        connection_factory: ConnectionFactory = _netmiko_connection_factory,
        credential_resolver: CredentialResolver | None = None,
        audit_logger: AuditLogger | None = None,
        host_keys: HostKeyStore | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._config = config
        self._targets = TargetResolver(config)
        self._platforms = PlatformRegistry(config.connections)
        self._policy = PolicyEngine(config.policy)
        self._connection_factory = connection_factory
        self._credentials = credential_resolver or PassBackend()
        self._audit = audit_logger or AuditLogger(config.policy.runtime.audit_file)
        self._host_keys = host_keys or HostKeyStore(config.policy.runtime.known_hosts_file)
        self._clock = clock
        self._sessions: dict[str, _ManagedSession] = {}
        self._lock = threading.RLock()

    def open_session(self, name: str | None = None, **ad_hoc: object) -> SessionInfo:
        """Open a direct SSH session and run Netmiko session preparation."""
        self._cleanup_expired()
        target = self._targets.resolve(name=name, **ad_hoc)
        platform = self._platforms.resolve(target.platform)
        profile = self._direct_profile(target)
        credentials = self._resolve_credentials(target)
        redactor = Redactor([credentials.password])
        port = target.port or profile.port or 22
        warnings = self._transport_warnings(profile)

        self._audit_event(
            "open_session",
            target=target,
            platform=platform,
            username=credentials.username,
            outcome="started",
            redactor=redactor,
        )
        connection: TerminalConnection | None = None
        try:
            host_key = self._host_keys.ensure(
                target.host,
                port=port,
                policy=profile.host_key_policy,
                timeout=float(self._config.policy.runtime.command_timeout),
            )
            warnings.extend(self._host_key_warnings(host_key))
            connection = self._connection_factory(
                self._connection_params(target, platform, credentials, port)
            )
            prompt = connection.find_prompt()
        except Exception as exc:
            if connection is not None:
                connection.disconnect()
            message = redactor.redact(str(exc))
            self._audit_event(
                "open_session",
                target=target,
                platform=platform,
                username=credentials.username,
                outcome="failed",
                redactor=redactor,
                error=message,
            )
            if isinstance(exc, TransportError):
                raise
            raise TransportError(f"failed to connect to {target.name}: {message}") from exc

        now = self._clock()
        wall_now = datetime.now(UTC)
        session = _ManagedSession(
            session_id=uuid.uuid4().hex,
            target=target,
            platform=platform,
            connection=connection,
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
                connection.disconnect()
                raise SessionError("maximum number of open sessions reached")
            self._sessions[session.session_id] = session
        self._audit_event(
            "open_session",
            target=target,
            platform=platform,
            username=credentials.username,
            outcome="completed",
            redactor=redactor,
            session_id=session.session_id,
            prompt=session.prompt,
            warnings=warnings,
        )
        return self._session_info(session)

    def run_command(self, session_id: str, command: str) -> CommandResult:
        """Execute a policy-allowed read-only command in a ready session."""
        session = self._get_session(session_id)
        with session.lock:
            self._require_ready(session)
            decision = self._policy.evaluate(command)
            if decision != "allow":
                self._audit_command(session, command, decision, "not_executed")
                if decision == "deny":
                    raise PolicyError("command is denied by policy")
                return CommandResult(
                    session_id=session_id,
                    command=session.redactor.redact(command),
                    policy=decision,
                    executed=False,
                    confirmation_required=True,
                )

            self._audit_command(session, command, decision, "started")
            try:
                raw_output = session.connection.send_command(
                    command,
                    read_timeout=float(self._config.policy.runtime.command_timeout),
                )
            except Exception as exc:
                session.state = SessionState.FAILED
                message = session.redactor.redact(str(exc))
                self._audit_command(session, command, decision, "failed", error=message)
                raise TransportError(f"command failed in session {session_id}: {message}") from exc

            output = session.redactor.redact(raw_output)
            offset = self._append_output(session, output)
            inline, truncated = _truncate_utf8(
                output, self._config.policy.runtime.max_inline_output_bytes
            )
            self._touch(session)
            self._audit_command(
                session,
                command,
                decision,
                "completed",
                output_bytes=len(output.encode()),
            )
            next_offset = offset + len(output) if truncated else None
            return CommandResult(
                session_id=session_id,
                command=session.redactor.redact(command),
                policy=decision,
                executed=True,
                output=inline,
                truncated=truncated,
                output_offset=offset,
                next_output_offset=next_offset,
            )

    def run_commands(self, session_id: str, commands: list[str]) -> list[CommandResult]:
        """Run commands serially and stop at the first unexecuted command."""
        session = self._get_session(session_id)
        with session.lock:
            results: list[CommandResult] = []
            for command in commands:
                result = self.run_command(session_id, command)
                results.append(result)
                if not result.executed:
                    break
            return results

    def read_output(
        self, session_id: str, *, offset: int = 0, limit: int | None = None
    ) -> OutputChunk:
        """Read a bounded slice of accumulated, redacted session output."""
        if offset < 0:
            raise SessionError("output offset must be non-negative")
        if limit is None:
            limit = self._config.policy.runtime.max_inline_output_bytes
        if limit <= 0:
            raise SessionError("output limit must be positive")
        session = self._get_session(session_id)
        with session.lock:
            if offset < session.output_start:
                raise SessionError(
                    "output before offset "
                    f"{session.output_start} has expired from the session buffer"
                )
            relative_offset = offset - session.output_start
            chunk = session.output[relative_offset : relative_offset + limit]
            next_offset = offset + len(chunk)
            buffer_end = session.output_start + len(session.output)
            return OutputChunk(
                session_id=session_id,
                offset=offset,
                output=chunk,
                next_offset=next_offset if next_offset < buffer_end else None,
                oldest_offset=session.output_start,
                truncated=session.output_truncated,
            )

    def session_status(self, session_id: str) -> SessionInfo:
        """Return current metadata for an active session."""
        session = self._get_session(session_id)
        with session.lock:
            return self._session_info(session)

    def close_session(self, session_id: str) -> SessionInfo:
        """Disconnect and permanently remove a session."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionError(f"unknown or expired session {session_id}")
        with session.lock:
            session.state = SessionState.CLOSING
            self._audit_event(
                "close_session",
                target=session.target,
                platform=session.platform,
                outcome="started",
                redactor=session.redactor,
                session_id=session_id,
            )
            try:
                session.connection.disconnect()
            finally:
                session.state = SessionState.CLOSED
                self._touch(session)
            self._audit_event(
                "close_session",
                target=session.target,
                platform=session.platform,
                outcome="completed",
                redactor=session.redactor,
                session_id=session_id,
            )
            return self._session_info(session)

    def _direct_profile(self, target: Target) -> DirectConnection:
        profile = self._config.connections.connections[target.connection]
        if not isinstance(profile, DirectConnection):
            raise TransportError(
                f"connection profile {target.connection!r} is {profile.type!r}; "
                "only direct SSH is implemented in this phase"
            )
        if profile.protocol == "telnet":
            raise TransportError("Telnet is not implemented in this phase")
        return profile

    def _resolve_credentials(self, target: Target) -> Credentials:
        profile = self._config.credentials.credentials[target.credentials]
        return self._credentials.resolve(profile)

    def _connection_params(
        self,
        target: Target,
        platform: Platform,
        credentials: Credentials,
        port: int,
    ) -> dict[str, object]:
        timeout = float(self._config.policy.runtime.command_timeout)
        return {
            "device_type": platform.driver,
            "host": target.host,
            "port": port,
            "username": credentials.username,
            "password": credentials.password,
            "conn_timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "fast_cli": False,
            "ssh_strict": True,
            "system_host_keys": False,
            "alt_host_keys": True,
            "alt_key_file": str(self._host_keys.path),
        }

    @staticmethod
    def _transport_warnings(profile: DirectConnection) -> list[str]:
        warnings: list[str] = []
        if profile.protocol == "legacy_ssh":
            warnings.append("legacy SSH profile in use")
        if profile.host_key_algorithms or profile.kex_algorithms or profile.ciphers:
            warnings.append("explicit legacy algorithm overrides are not implemented yet")
        return warnings

    @staticmethod
    def _host_key_warnings(status: HostKeyStatus) -> list[str]:
        if status.enrolled:
            return [
                "host key enrolled with TOFU: "
                f"{status.algorithm} {status.fingerprint}; switch the profile to strict"
            ]
        return []

    def _get_session(self, session_id: str) -> _ManagedSession:
        self._cleanup_expired()
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"unknown or expired session {session_id}")
        return session

    def _require_ready(self, session: _ManagedSession) -> None:
        if session.state != SessionState.READY:
            raise SessionError(
                f"session {session.session_id} is not ready: {session.state.value}"
            )

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
                session.connection.disconnect()
                self._touch(session)
                self._audit_event(
                    "close_session",
                    target=session.target,
                    platform=session.platform,
                    outcome="expired",
                    redactor=session.redactor,
                    session_id=session.session_id,
                )

    def _append_output(self, session: _ManagedSession, output: str) -> int:
        output_offset = session.output_start + len(session.output)
        combined = session.output + output
        trimmed, dropped = _keep_utf8_tail(
            combined, self._config.policy.runtime.max_session_buffer_bytes
        )
        if dropped:
            session.output_start += dropped
            session.output_truncated = True
        session.output = trimmed
        return output_offset

    def _touch(self, session: _ManagedSession) -> None:
        session.last_used_monotonic = self._clock()
        session.last_used_at = datetime.now(UTC)

    def _session_info(self, session: _ManagedSession) -> SessionInfo:
        return SessionInfo(
            session_id=session.session_id,
            target=session.target.name,
            host=session.target.host,
            platform=session.platform.name,
            dialect=session.platform.dialect,
            prompt=session.prompt,
            state=session.state,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            warnings=list(session.warnings),
        )

    def _audit_command(
        self,
        session: _ManagedSession,
        command: str,
        decision: Action,
        outcome: str,
        **details: object,
    ) -> None:
        self._audit_event(
            "run_command",
            target=session.target,
            platform=session.platform,
            outcome=outcome,
            redactor=session.redactor,
            session_id=session.session_id,
            command=session.redactor.redact(command),
            policy=decision,
            **details,
        )

    def _audit_event(
        self,
        event: str,
        *,
        target: Target,
        platform: Platform,
        outcome: str,
        redactor: Redactor,
        **details: object,
    ) -> None:
        record: dict[str, object] = {
            "event": event,
            "outcome": outcome,
            "target": target.name,
            "host": target.host,
            "platform": platform.name,
            "dialect": platform.dialect,
            "connection_profile": target.connection,
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
