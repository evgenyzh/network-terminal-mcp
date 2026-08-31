"""Direct SSH session manager backed by Netmiko."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypedDict, cast

import paramiko
from netmiko import ConnectHandler
from netmiko.ssh_dispatcher import redispatch

from network_terminal_mcp.audit import AuditLogger
from network_terminal_mcp.changes.manager import ChangeManager
from network_terminal_mcp.changes.models import (
    ChangeResult,
    ChangeState,
    SafetyNet,
)
from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import (
    Action,
    ConnectionProfile,
    ConsoleConnection,
    CredentialProfile,
    DirectConnection,
    NestedConnection,
    ProxyJumpConnection,
)
from network_terminal_mcp.credentials.pass_backend import Credentials, PassBackend
from network_terminal_mcp.errors import ChangeError, PolicyError, SessionError, TransportError
from network_terminal_mcp.host_keys import HostKeyStatus, HostKeyStore, probe_host_key_socket
from network_terminal_mcp.platforms import Platform, PlatformRegistry
from network_terminal_mcp.policy.engine import PolicyEngine, check_structural_safety
from network_terminal_mcp.redaction import Redactor
from network_terminal_mcp.sessions.models import (
    CliHelpResult,
    CommandResult,
    ControlResult,
    OutputChunk,
    ResponseResult,
    SessionInfo,
    SessionState,
)
from network_terminal_mcp.targets.resolver import Target, TargetResolver


class TerminalConnection(Protocol):
    """Subset of Netmiko's connection API required by the manager."""

    def find_prompt(self) -> str: ...

    def send_command(
        self,
        command_string: str,
        *,
        expect_string: str | None = None,
        read_timeout: float,
        strip_prompt: bool = True,
        strip_command: bool = True,
        cmd_verify: bool = True,
    ) -> str: ...

    def write_channel(self, out_data: str) -> None: ...

    def read_until_pattern(self, pattern: str, *, read_timeout: float) -> str: ...

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str: ...

    def disconnect(self) -> None: ...


class CredentialResolver(Protocol):
    """Credential backend abstraction used by tests and the real server."""

    def resolve(self, profile: CredentialProfile) -> Credentials: ...


ConnectionFactory = Callable[[dict[str, object]], TerminalConnection]
NestedRedispatch = Callable[[TerminalConnection, str], None]
Clock = Callable[[], float]

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PAGER_END = re.compile(
    r"(?:--more--|<--- more --->|---- more ----|press any key to continue|"
    r"---\s*\(\s*more\s+\d+\s*%?\s*\)\s*---)\s*\Z",
    re.IGNORECASE,
)
_CONFIRMATION_END = re.compile(
    r"(?:\b(?:continue|proceed|confirm|are you sure|do you want|really|overwrite|"
    r"delete|save|reset|reboot|reload)\b[^\r\n]{0,160}"
    r"(?:\[y/n\]|\(y/n\)|\[yes/no\]|\(yes/no\)))\s*\Z",
    re.IGNORECASE,
)
_SECRET_END = re.compile(r"(?:password|passphrase|secret)\s*:\s*\Z", re.IGNORECASE)

# Full algorithm sets Paramiko can offer; per-profile allowlists become the
# complement via ``disabled_algorithms`` so legacy overrides are never global.
_PARAMIKO_ALGORITHMS: dict[str, list[str]] = {
    "keys": sorted(paramiko.Transport._preferred_keys),  # type: ignore[attr-defined]
    "kex": sorted(paramiko.Transport._preferred_kex),  # type: ignore[attr-defined]
    "ciphers": sorted(paramiko.Transport._preferred_ciphers),  # type: ignore[attr-defined]
}


@dataclass(frozen=True)
class _PendingResponse:
    prompt: str
    allowed_responses: tuple[str, ...]


@dataclass(frozen=True)
class _TerminalOutcome:
    output: str
    pager_active: bool = False
    response_required: bool = False
    device_prompt: str | None = None
    allowed_responses: tuple[str, ...] = ()


class _OutputFields(TypedDict):
    output: str
    truncated: bool
    output_offset: int | None
    next_output_offset: int | None
    pager_active: bool
    response_required: bool
    device_prompt: str | None
    allowed_responses: list[str]


def _netmiko_connection_factory(params: dict[str, object]) -> TerminalConnection:
    return cast(TerminalConnection, ConnectHandler(**params))


def _netmiko_nested_redispatch(connection: TerminalConnection, device_type: str) -> None:
    redispatch(cast(Any, connection), device_type, session_prep=True)


def _disabled_algorithms(profile: DirectConnection) -> dict[str, list[str]] | None:
    """Convert per-profile allowlists into Paramiko ``disabled_algorithms``.

    Only categories explicitly listed in the profile are restricted; the
    remaining categories keep Paramiko's default algorithm set. Returns None
    when the profile does not restrict anything.
    """
    allowlists: dict[str, list[str] | None] = {
        "keys": profile.host_key_algorithms,
        "kex": profile.kex_algorithms,
        "ciphers": profile.ciphers,
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
    target: Target
    platform: Platform
    connection: TerminalConnection
    jump_client: paramiko.SSHClient | None
    prompt: str
    raw_prompt: str
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
    pager_pages: int = 0
    pager_is_help: bool = False
    pending_response: _PendingResponse | None = None
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
        jump_client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
        nested_redispatch: NestedRedispatch = _netmiko_nested_redispatch,
        change_manager: ChangeManager | None = None,
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
        self._jump_client_factory = jump_client_factory
        self._nested_redispatch = nested_redispatch
        self._changes = change_manager or ChangeManager()
        self._clock = clock
        self._sessions: dict[str, _ManagedSession] = {}
        self._lock = threading.RLock()

    def open_session(self, name: str | None = None, **ad_hoc: object) -> SessionInfo:
        """Open a direct or configured one-hop SSH session."""
        self._cleanup_expired()
        target = self._targets.resolve(name=name, **ad_hoc)
        platform = self._platforms.resolve(target.platform)
        profile = self._transport_profile(target)
        credentials = self._resolve_credentials(target.credentials)
        intermediate_credentials = (
            self._resolve_credentials(profile.credentials)
            if isinstance(profile, NestedConnection)
            else None
        )
        jump_credentials = (
            self._resolve_credentials(profile.jump_credentials)
            if isinstance(profile, ProxyJumpConnection)
            else None
        )
        passwords = [
            secret
            for secret in (credentials.password, credentials.key_passphrase)
            if secret is not None
        ]
        if intermediate_credentials is not None:
            passwords.extend(
                secret
                for secret in (
                    intermediate_credentials.password,
                    intermediate_credentials.key_passphrase,
                )
                if secret is not None
            )
        if jump_credentials is not None:
            passwords.extend(
                secret
                for secret in (jump_credentials.password, jump_credentials.key_passphrase)
                if secret is not None
            )
        redactor = Redactor(passwords)
        port = target.port or profile.port or 22
        if isinstance(profile, NestedConnection):
            if profile.next_protocol == "telnet":
                port = target.port or profile.next_port or 23
            else:
                port = target.port or profile.next_port or 22
        if isinstance(profile, DirectConnection) and profile.protocol == "telnet":
            port = target.port or profile.port or 23
        if isinstance(profile, ConsoleConnection):
            console_port = target.port or profile.port
            if console_port is None:
                raise TransportError("console connection requires a port")
            port = console_port
        warnings = self._transport_warnings(profile)
        route = self._route_details(profile)

        self._audit_event(
            "open_session",
            target=target,
            platform=platform,
            username=credentials.username,
            outcome="started",
            redactor=redactor,
            **route,
        )
        connection: TerminalConnection | None = None
        jump_client: paramiko.SSHClient | None = None
        try:
            timeout = float(self._config.policy.runtime.command_timeout)
            if isinstance(profile, ProxyJumpConnection):
                assert jump_credentials is not None
                jump_host_key = self._host_keys.ensure(
                    profile.jump_host,
                    port=profile.jump_port,
                    policy=profile.jump_host_key_policy,
                    timeout=timeout,
                )
                warnings.extend(
                    f"jump host: {warning}" for warning in self._host_key_warnings(jump_host_key)
                )
                jump_client = self._open_jump_client(profile, jump_credentials)
                host_key = self._host_keys.ensure(
                    target.host,
                    port=port,
                    policy=profile.host_key_policy,
                    timeout=timeout,
                    probe=lambda host, probe_port, probe_timeout: self._probe_via_jump(
                        jump_client, host, probe_port, probe_timeout
                    ),
                )
                sock = self._open_jump_channel(jump_client, target.host, port)
            elif isinstance(profile, NestedConnection):
                assert intermediate_credentials is not None
                intermediate_host_key = self._host_keys.ensure(
                    profile.host,
                    port=profile.port or 22,
                    policy=profile.host_key_policy,
                    timeout=timeout,
                )
                warnings.extend(
                    f"intermediate host: {warning}"
                    for warning in self._host_key_warnings(intermediate_host_key)
                )
                connection = self._connection_factory(
                    self._nested_connection_params(profile, intermediate_credentials)
                )
                if profile.next_protocol == "telnet":
                    self._nested_telnet_login(connection, target, credentials, port)
                else:
                    self._nested_login(connection, target, credentials, port)
                self._nested_redispatch(connection, platform.driver)
                host_key = None
                sock = None
            elif isinstance(profile, DirectConnection) and profile.protocol == "telnet":
                self._require_telnet_allowed(target)
                connection = self._connection_factory(
                    self._telnet_connection_params(target, platform, credentials, port)
                )
                host_key = None
                sock = None
            elif isinstance(profile, ConsoleConnection):
                self._require_telnet_allowed(target)
                connection = self._connection_factory(
                    self._telnet_connection_params(target, platform, credentials, port)
                )
                host_key = None
                sock = None
            else:
                host_key = self._host_keys.ensure(
                    target.host,
                    port=port,
                    policy=profile.host_key_policy,
                    timeout=timeout,
                )
                sock = None
            if host_key is not None:
                warnings.extend(self._host_key_warnings(host_key))
            if connection is None:
                connection = self._connection_factory(
                    self._connection_params(
                        target, platform, credentials, port, sock=sock, profile=profile
                    )
                )
            prompt = connection.find_prompt()
        except Exception as exc:
            try:
                if connection is not None:
                    connection.disconnect()
            finally:
                self._close_jump_client(jump_client)
            message = redactor.redact(str(exc))
            self._audit_event(
                "open_session",
                target=target,
                platform=platform,
                username=credentials.username,
                outcome="failed",
                redactor=redactor,
                error=message,
                **route,
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
            jump_client=jump_client,
            prompt=redactor.redact(prompt),
            raw_prompt=prompt,
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
            target=target,
            platform=platform,
            username=credentials.username,
            outcome="completed",
            redactor=redactor,
            session_id=session.session_id,
            prompt=session.prompt,
            warnings=warnings,
            **route,
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
                outcome = self._consume_terminal_output(
                    session, self._send_command_until_terminal(session, command)
                )
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_command(session, command, decision, "failed", error=message)
                raise TransportError(f"command failed in session {session_id}: {message}") from exc

            output_fields = self._record_terminal_output(session, outcome)
            self._touch(session)
            self._audit_command(
                session,
                command,
                decision,
                self._terminal_outcome_label(outcome),
                output_bytes=self._output_bytes(session, outcome.output),
            )
            return CommandResult(
                session_id=session_id,
                command=session.redactor.redact(command),
                policy=decision,
                executed=True,
                **output_fields,
            )

    def run_commands(self, session_id: str, commands: list[str]) -> list[CommandResult]:
        """Run commands serially and stop at the first unexecuted command."""
        session = self._get_session(session_id)
        with session.lock:
            results: list[CommandResult] = []
            for command in commands:
                result = self.run_command(session_id, command)
                results.append(result)
                if not result.executed or result.pager_active or result.response_required:
                    break
            return results

    def cli_help(self, session_id: str, line: str) -> CliHelpResult:
        """Read completion help, then cancel the unfinished line before returning."""
        session = self._get_session(session_id)
        with session.lock:
            self._require_ready(session)
            decision = self._policy.evaluate_cli_help(line)
            if decision != "allow":
                self._audit_interaction(
                    session,
                    "cli_help",
                    "not_executed",
                    line=line,
                    policy=decision,
                )
                if decision == "deny":
                    raise PolicyError("CLI help is denied by policy")
                return CliHelpResult(
                    session_id=session_id,
                    line=session.redactor.redact(line),
                    policy=decision,
                    executed=False,
                    confirmation_required=True,
                )

            self._audit_interaction(
                session, "cli_help", "started", line=line, policy=decision
            )
            help_timeout = float(self._config.policy.runtime.cli_help_timeout)
            try:
                if session.platform.cli_help_requires_enter:
                    # Some CLIs (e.g. D-Link) only render the help list after
                    # Enter. The appended '?' keeps the request non-executing.
                    outcome = self._consume_terminal_output(
                        session,
                        self._send_command_until_terminal(
                            session, f"{line}?", cmd_verify=False
                        ),
                    )
                else:
                    raw_output = self._read_help(session, line)
                    visible = _visible_terminal_text(raw_output)
                    if self._ends_with_pager(visible):
                        session.state = SessionState.PAGING
                        session.pending_response = None
                        session.pager_pages = 1
                        session.pager_is_help = True
                        outcome = _TerminalOutcome(output=raw_output, pager_active=True)
                    elif self._ends_with_secret_prompt(visible):
                        raise SessionError("device requested a secret during CLI help")
                    elif self._ends_with_help_prompt(session, visible):
                        self._clear_help_tail(session)
                        outcome = _TerminalOutcome(
                            output=self._strip_help_prompt(session, raw_output)
                        )
                    else:
                        self._return_to_prompt(session, timeout=help_timeout)
                        outcome = _TerminalOutcome(output=raw_output)
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_interaction(
                    session, "cli_help", "failed", line=line, policy=decision, error=message
                )
                raise TransportError(f"CLI help failed in session {session_id}: {message}") from exc

            output_fields = self._record_terminal_output(session, outcome)
            self._touch(session)
            self._audit_interaction(
                session,
                "cli_help",
                self._terminal_outcome_label(outcome),
                line=line,
                policy=decision,
                output_bytes=self._output_bytes(session, outcome.output),
            )
            return CliHelpResult(
                session_id=session_id,
                line=session.redactor.redact(line),
                policy=decision,
                executed=True,
                **output_fields,
            )

    def send_control(self, session_id: str, action: str) -> ControlResult:
        """Continue or cancel a pager, or cancel a recognized device prompt."""
        session = self._get_session(session_id)
        with session.lock:
            if action == "space":
                self._require_state(session, SessionState.PAGING, action)
                return self._continue_pager(session, action)
            if action == "q":
                self._require_state(session, SessionState.PAGING, action)
                return self._cancel_interaction(session, action, "pager")
            if action == "ctrl-c":
                if session.state not in (SessionState.PAGING, SessionState.AWAITING_RESPONSE):
                    raise SessionError(
                        f"control {action!r} is not allowed while session "
                        f"{session.session_id} is {session.state.value}"
                    )
                return self._cancel_interaction(session, action, "interactive prompt")
            raise SessionError(f"unsupported control action: {action!r}")

    def respond(self, session_id: str, response: str) -> ResponseResult:
        """Send one allowlisted response to the pending device confirmation prompt."""
        session = self._get_session(session_id)
        with session.lock:
            self._require_state(session, SessionState.AWAITING_RESPONSE, "respond")
            pending = session.pending_response
            if pending is None:
                self._fail_session(session)
                raise SessionError("session lost its pending response metadata")
            normalized = response.lower()
            if normalized not in pending.allowed_responses:
                self._audit_interaction(
                    session,
                    "respond",
                    "denied",
                    response=response,
                    allowed_responses=list(pending.allowed_responses),
                )
                raise PolicyError("response is not allowed by the pending device prompt")

            self._audit_interaction(session, "respond", "started", response=normalized)
            try:
                outcome = self._consume_terminal_output(
                    session,
                    self._send_command_until_terminal(session, normalized, cmd_verify=False),
                )
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_interaction(
                    session, "respond", "failed", response=normalized, error=message
                )
                raise TransportError(f"response failed in session {session_id}: {message}") from exc

            output_fields = self._record_terminal_output(session, outcome)
            self._touch(session)
            self._audit_interaction(
                session,
                "respond",
                self._terminal_outcome_label(outcome),
                response=normalized,
                output_bytes=self._output_bytes(session, outcome.output),
            )
            return ResponseResult(
                session_id=session_id,
                response=normalized,
                **output_fields,
            )

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

    def close_session(self, session_id: str, *, force: bool = False) -> SessionInfo:
        """Disconnect and permanently remove a session.

        Refuses to close while a scheduled reboot from an applied change plan
        is still pending cancellation; ``force=True`` overrides for emergency
        cases and logs a ``reboot_not_cancelled`` audit warning.
        """
        active = self._changes.has_active_reboot(session_id)
        if active is not None and not force:
            raise SessionError(
                f"session {session_id} has an active scheduled reboot from change "
                f"plan {active.change_id}; call finalize_change first or "
                "close with force=true"
            )
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
                self._close_jump_client(session.jump_client)
                session.jump_client = None
                session.state = SessionState.CLOSED
                self._touch(session)
            outcome = "completed"
            if active is not None:
                self._audit_event(
                    "reboot_not_cancelled",
                    target=session.target,
                    platform=session.platform,
                    outcome="warning",
                    redactor=session.redactor,
                    session_id=session_id,
                    change_id=active.change_id,
                )
                outcome = "completed_with_reboot_pending"
            self._audit_event(
                "close_session",
                target=session.target,
                platform=session.platform,
                outcome=outcome,
                redactor=session.redactor,
                session_id=session_id,
            )
            return self._session_info(session)

    def plan_change(
        self,
        session_id: str,
        title: str,
        commands: list[str],
        *,
        safety_net: dict[str, str] | None = None,
        auto_approve: bool = False,
    ) -> ChangeResult:
        """Register a configuration change plan without executing anything."""
        if not title.strip():
            raise ChangeError("change plan title must not be empty")
        if not commands:
            raise ChangeError("change plan must contain at least one command")
        session = self._get_session(session_id)
        with session.lock:
            self._require_ready(session)
            self._require_writes_allowed(session)
            for command in commands:
                check_structural_safety(command, tool="plan_change")
            resolved_safety = None
            if safety_net is not None:
                if set(safety_net) != {"save", "arm", "cancel"}:
                    raise ChangeError(
                        "safety_net requires exactly save, arm and cancel commands"
                    )
                for command in safety_net.values():
                    check_structural_safety(command, tool="safety_net")
                resolved_safety = SafetyNet(**safety_net)
            plan = self._changes.create(
                session_id=session_id,
                target=session.target.name,
                host=session.target.host,
                platform=session.platform.name,
                title=title.strip(),
                commands=commands,
                safety_net=resolved_safety,
                auto_approve=auto_approve,
            )
            self._audit_event(
                "plan_change",
                target=session.target,
                platform=session.platform,
                outcome="created",
                redactor=session.redactor,
                session_id=session_id,
                change_id=plan.change_id,
                title=plan.title,
                hash=plan.hash,
                safety_net=bool(resolved_safety),
            )
            return ChangeResult.from_plan(plan)

    def apply_change(self, change_id: str) -> ChangeResult:
        """Confirm and then execute a change plan in two steps.

        The first call returns the canonical command list with
        ``confirmation_required`` and executes nothing. The second call on the
        same plan executes it. ``auto_approve`` plans skip the confirmation
        step only when the operator pre-authorized auto-approval for this
        session/device pair.
        """
        plan = self._changes.get(change_id)
        session = self._get_session(plan.session_id)
        with session.lock:
            if plan.state == ChangeState.PROPOSED:
                confirmed = self._changes.confirm(change_id)
                self._audit_event(
                    "apply_change",
                    target=session.target,
                    platform=session.platform,
                    outcome="confirmed",
                    redactor=session.redactor,
                    session_id=session.session_id,
                    change_id=change_id,
                    hash=plan.hash,
                )
                if not plan.auto_approve:
                    return ChangeResult.from_plan(
                        confirmed, confirmation_required=True
                    )
                plan = confirmed
            elif plan.state != ChangeState.CONFIRMED:
                raise ChangeError(
                    f"change plan {change_id} cannot be applied from {plan.state.value}"
                )

            self._require_ready(session)
            self._require_writes_allowed(session)
            warnings: list[str] = []
            output_parts: list[str] = []
            try:
                plan = self._changes.mark_applied(change_id)
                if plan.safety_net is not None:
                    self._run_change_command(
                        session, plan.safety_net.save, output_parts
                    )
                    self._run_change_command(
                        session, plan.safety_net.arm, output_parts
                    )
                    self._changes.set_reboot_required(change_id, True)
                    plan = self._changes.get(change_id)
                    warnings.append(
                        "scheduled reboot armed; call finalize_change after "
                        "verification to cancel it"
                    )
                for command in [cmd.command for cmd in plan.commands]:
                    self._run_change_command(session, command, output_parts)
            except Exception as exc:
                self._changes.mark_failed(change_id)
                message = session.redactor.redact(str(exc))
                self._audit_event(
                    "apply_change",
                    target=session.target,
                    platform=session.platform,
                    outcome="failed",
                    redactor=session.redactor,
                    session_id=session.session_id,
                    change_id=change_id,
                    hash=plan.hash,
                    error=message,
                )
                return ChangeResult.from_plan(
                    self._changes.get(change_id),
                    output="\n".join(output_parts),
                    error=message,
                )

            self._touch(session)
            output = "\n".join(output_parts)
            self._audit_event(
                "apply_change",
                target=session.target,
                platform=session.platform,
                outcome="completed",
                redactor=session.redactor,
                session_id=session.session_id,
                change_id=change_id,
                hash=plan.hash,
                reboot_cancel_required=plan.reboot_cancel_required,
            )
            return ChangeResult.from_plan(
                self._changes.get(change_id), output=output, warnings=warnings
            )

    def abort_change(self, change_id: str) -> ChangeResult:
        """Cancel a change plan before anything is executed."""
        plan = self._changes.get(change_id)
        session = self._get_session(plan.session_id)
        with session.lock:
            aborted = self._changes.abort(change_id)
            self._audit_event(
                "abort_change",
                target=session.target,
                platform=session.platform,
                outcome="aborted",
                redactor=session.redactor,
                session_id=session.session_id,
                change_id=change_id,
                hash=plan.hash,
            )
            return ChangeResult.from_plan(aborted)

    def finalize_change(self, change_id: str) -> ChangeResult:
        """Cancel the scheduled reboot or commit the safety net after apply."""
        plan = self._changes.get(change_id)
        if plan.state != ChangeState.APPLIED:
            raise ChangeError(
                f"change plan {change_id} cannot be finalized from {plan.state.value}"
            )
        session = self._get_session(plan.session_id)
        with session.lock:
            self._require_ready(session)
            warnings: list[str] = []
            output_parts: list[str] = []
            error: str | None = None
            try:
                if plan.safety_net is not None and plan.reboot_cancel_required:
                    self._run_change_command(
                        session, plan.safety_net.cancel, output_parts
                    )
                    self._changes.set_reboot_required(change_id, False)
            except Exception as exc:
                self._changes.mark_failed(change_id)
                error = session.redactor.redact(str(exc))
                warnings.append(
                    "could not cancel the scheduled reboot; the device will "
                    "reboot on its own (reload cancel command failed)"
                )
                self._audit_event(
                    "finalize_change",
                    target=session.target,
                    platform=session.platform,
                    outcome="failed",
                    redactor=session.redactor,
                    session_id=session.session_id,
                    change_id=change_id,
                    hash=plan.hash,
                    error=error,
                )
                return ChangeResult.from_plan(
                    self._changes.get(change_id),
                    output="\n".join(output_parts),
                    warnings=warnings,
                    error=error,
                )
            finalized = self._changes.finalize(change_id)
            self._touch(session)
            self._audit_event(
                "finalize_change",
                target=session.target,
                platform=session.platform,
                outcome="completed",
                redactor=session.redactor,
                session_id=session.session_id,
                change_id=change_id,
                hash=plan.hash,
                reboot_cancel_required=False,
            )
            return ChangeResult.from_plan(
                finalized, output="\n".join(output_parts), warnings=warnings
            )

    def _require_writes_allowed(self, session: _ManagedSession) -> None:
        if not session.target.allow_writes:
            raise ChangeError(
                f"changes are not enabled for target {session.target.name!r}; "
                "set allow_writes: true on the device"
            )
        if self._config.policy.defaults.write_change == "deny":
            raise ChangeError("change plans are denied by policy defaults")

    def _run_change_command(
        self,
        session: _ManagedSession,
        command: str,
        output_parts: list[str],
    ) -> None:
        """Execute one change command, appending its output to the result."""
        check_structural_safety(command, tool="plan_change")
        self._require_ready(session)
        raw_output = self._send_command_until_terminal(session, command)
        outcome = self._consume_terminal_output(session, raw_output)
        if outcome.pager_active:
            raise TransportError(
                "change command entered a pager; refusing to continue"
            )
        if outcome.response_required:
            raise TransportError(
                "change command triggered a device confirmation; refusing to "
                "continue"
            )
        output_parts.append(session.redactor.redact(outcome.output))

    def _continue_pager(self, session: _ManagedSession, action: str) -> ControlResult:
        if session.pager_pages >= self._config.policy.runtime.max_pager_pages:
            self._audit_interaction(
                session,
                "send_control",
                "started",
                action="q",
                reason="pager_page_limit",
            )
            try:
                if session.pager_is_help:
                    session.connection.write_channel("q")
                    output = self._finalize_help_pager(session)
                else:
                    output = self._send_control_until_prompt(session, "q")
            except Exception as exc:
                self._fail_session(session)
                message = session.redactor.redact(str(exc))
                self._audit_interaction(
                    session,
                    "send_control",
                    "failed",
                    action="q",
                    error=message,
                )
                raise TransportError(
                    f"pager abort failed in session {session.session_id}: {message}"
                ) from exc
            outcome = _TerminalOutcome(output=output)
            output_fields = self._record_terminal_output(session, outcome)
            self._touch(session)
            self._audit_interaction(
                session,
                "send_control",
                "pager_limit_reached",
                action="q",
                output_bytes=self._output_bytes(session, output),
            )
            return ControlResult(session_id=session.session_id, action="q", **output_fields)

        self._audit_interaction(session, "send_control", "started", action=action)
        try:
            session.connection.write_channel(" ")
            raw_output = self._read_until_terminal(session)
            if session.pager_is_help:
                outcome = self._page_help(session, raw_output)
            else:
                outcome = self._consume_terminal_output(session, raw_output)
        except Exception as exc:
            self._fail_session(session)
            message = session.redactor.redact(str(exc))
            self._audit_interaction(
                session, "send_control", "failed", action=action, error=message
            )
            raise TransportError(
                f"pager continuation failed in session {session.session_id}: {message}"
            ) from exc

        output_fields = self._record_terminal_output(session, outcome)
        self._touch(session)
        self._audit_interaction(
            session,
            "send_control",
            self._terminal_outcome_label(outcome),
            action=action,
            output_bytes=self._output_bytes(session, outcome.output),
        )
        return ControlResult(session_id=session.session_id, action="space", **output_fields)

    def _page_help(self, session: _ManagedSession, raw_output: str) -> _TerminalOutcome:
        """Classify one help pager page and finalize on the last one."""
        visible = _visible_terminal_text(raw_output)
        if self._ends_with_pager(visible):
            session.state = SessionState.PAGING
            session.pager_pages += 1
            return _TerminalOutcome(output=raw_output, pager_active=True)
        if self._ends_with_secret_prompt(visible):
            raise SessionError("device requested a password, passphrase, or secret")
        if self._ends_with_help_prompt(session, visible):
            self._finalize_help_pager(session, prompt_already_seen=True)
            return _TerminalOutcome(output=self._strip_help_prompt(session, raw_output))
        raise SessionError("help pager did not return to a recognized prompt")

    def _finalize_help_pager(
        self, session: _ManagedSession, *, prompt_already_seen: bool = False
    ) -> str:
        """Wait for the help prompt tail, then clear the residual typed line."""
        help_timeout = float(self._config.policy.runtime.cli_help_timeout)
        output = ""
        if not prompt_already_seen:
            output = session.connection.read_until_pattern(
                self._help_prompt_tail_pattern(session),
                read_timeout=help_timeout,
            )
        self._clear_help_tail(session)
        return self._strip_help_prompt(session, output)

    def _cancel_interaction(
        self, session: _ManagedSession, action: str, description: str
    ) -> ControlResult:
        self._audit_interaction(session, "send_control", "started", action=action)
        try:
            if session.pager_is_help:
                session.connection.write_channel("q" if action == "q" else "\x03")
                output = self._finalize_help_pager(session)
            else:
                output = self._send_control_until_prompt(
                    session, "q" if action == "q" else "\x03"
                )
        except Exception as exc:
            self._fail_session(session)
            message = session.redactor.redact(str(exc))
            self._audit_interaction(
                session, "send_control", "failed", action=action, error=message
            )
            raise TransportError(
                f"failed to cancel {description} in session {session.session_id}: {message}"
            ) from exc

        outcome = _TerminalOutcome(output=output)
        output_fields = self._record_terminal_output(session, outcome)
        self._touch(session)
        self._audit_interaction(
            session,
            "send_control",
            "completed",
            action=action,
            output_bytes=self._output_bytes(session, output),
        )
        return ControlResult(
            session_id=session.session_id,
            action=cast(Literal["space", "q", "ctrl-c"], action),
            **output_fields,
        )

    def _send_command_until_terminal(
        self, session: _ManagedSession, command: str, *, cmd_verify: bool = True
    ) -> str:
        return session.connection.send_command(
            command,
            expect_string=self._terminal_end_pattern(session),
            read_timeout=float(self._config.policy.runtime.command_timeout),
            strip_prompt=False,
            strip_command=True,
            cmd_verify=cmd_verify,
        )

    def _read_until_terminal(self, session: _ManagedSession) -> str:
        return session.connection.read_until_pattern(
            self._terminal_end_pattern(session),
            read_timeout=float(self._config.policy.runtime.command_timeout),
        )

    def _read_help(self, session: _ManagedSession, line: str) -> str:
        """Send a non-Enter help request and read the first screen.

        The caller decides whether the output is paged. A failed read still
        tries to restore the prompt because the line is unsafe to reuse.
        """
        help_timeout = float(self._config.policy.runtime.cli_help_timeout)
        session.connection.write_channel(f"{line}?")
        try:
            return session.connection.read_channel_timing(
                last_read=0.2,
                read_timeout=help_timeout,
            )
        except Exception:
            self._return_to_prompt(session, timeout=help_timeout)
            raise

    def _return_to_prompt(self, session: _ManagedSession, *, timeout: float | None = None) -> None:
        self._send_control_until_prompt(session, "\x03", timeout=timeout)

    @staticmethod
    def _clear_help_tail(session: _ManagedSession) -> None:
        """Cancel a line after its prompt has already been observed."""
        session.connection.write_channel("\x03")
        # Junos retains the line after Ctrl-C; Ctrl-U clears it without Enter.
        session.connection.write_channel("\x15")
        session.state = SessionState.READY
        session.pending_response = None
        session.pager_pages = 0
        session.pager_is_help = False

    def _send_control_until_prompt(
        self, session: _ManagedSession, control: str, *, timeout: float | None = None
    ) -> str:
        session.connection.write_channel(control)
        output = session.connection.read_until_pattern(
            self._prompt_end_pattern(session),
            read_timeout=(
                timeout
                if timeout is not None
                else float(self._config.policy.runtime.command_timeout)
            ),
        )
        session.state = SessionState.READY
        session.pending_response = None
        session.pager_pages = 0
        session.pager_is_help = False
        return self._strip_prompt(session, output)

    def _consume_terminal_output(
        self, session: _ManagedSession, raw_output: str
    ) -> _TerminalOutcome:
        visible = _visible_terminal_text(raw_output)
        if self._ends_with_secret_prompt(visible):
            raise SessionError("device requested a password, passphrase, or secret")
        if self._ends_with_pager(visible):
            session.state = SessionState.PAGING
            session.pending_response = None
            session.pager_pages += 1
            return _TerminalOutcome(output=raw_output, pager_active=True)
        if self._ends_with_confirmation(visible):
            prompt = _last_terminal_line(raw_output)
            allowed_responses = _allowed_responses(visible)
            session.state = SessionState.AWAITING_RESPONSE
            session.pager_pages = 0
            session.pending_response = _PendingResponse(prompt, allowed_responses)
            return _TerminalOutcome(
                output=raw_output,
                response_required=True,
                device_prompt=prompt,
                allowed_responses=allowed_responses,
            )
        if not self._ends_with_prompt(session, visible):
            raise SessionError("terminal output did not end at a recognized prompt")

        session.state = SessionState.READY
        session.pending_response = None
        session.pager_pages = 0
        return _TerminalOutcome(output=self._strip_prompt(session, raw_output))

    def _record_terminal_output(
        self, session: _ManagedSession, outcome: _TerminalOutcome
    ) -> _OutputFields:
        output = session.redactor.redact(outcome.output)
        offset = self._append_output(session, output)
        inline, truncated = _truncate_utf8(
            output, self._config.policy.runtime.max_inline_output_bytes
        )
        return {
            "output": inline,
            "truncated": truncated,
            "output_offset": offset,
            "next_output_offset": offset + len(output) if truncated else None,
            "pager_active": outcome.pager_active,
            "response_required": outcome.response_required,
            "device_prompt": (
                session.redactor.redact(outcome.device_prompt)
                if outcome.device_prompt is not None
                else None
            ),
            "allowed_responses": list(outcome.allowed_responses),
        }

    @staticmethod
    def _output_bytes(session: _ManagedSession, output: str) -> int:
        return len(session.redactor.redact(output).encode())

    @staticmethod
    def _terminal_outcome_label(outcome: _TerminalOutcome) -> str:
        if outcome.pager_active:
            return "paging"
        if outcome.response_required:
            return "awaiting_response"
        return "completed"

    @staticmethod
    def _require_state(
        session: _ManagedSession, expected: SessionState, action: str
    ) -> None:
        if session.state != expected:
            raise SessionError(
                f"control {action!r} requires {expected.value}; session "
                f"{session.session_id} is {session.state.value}"
            )

    @staticmethod
    def _fail_session(session: _ManagedSession) -> None:
        session.state = SessionState.FAILED
        session.pending_response = None
        session.pager_pages = 0
        session.pager_is_help = False

    def _terminal_end_pattern(self, session: _ManagedSession) -> str:
        prompt = self._prompt_end_pattern(session)
        if session.pager_is_help:
            # During help paging the device returns to the prompt with the
            # unfinished line still typed (e.g. "switch>show "). Match it.
            prompt = rf"(?:{prompt}|{self._help_prompt_tail_pattern(session)})"
        return (
            rf"(?:{prompt}|(?i:{_PAGER_END.pattern})|"
            rf"(?i:{_CONFIRMATION_END.pattern})|(?i:{_SECRET_END.pattern}))"
        )

    @staticmethod
    def _prompt_end_pattern(session: _ManagedSession) -> str:
        return rf"{re.escape(session.raw_prompt)}\s*\Z"

    @staticmethod
    def _help_prompt_tail_pattern(session: _ManagedSession) -> str:
        return rf"{re.escape(session.raw_prompt)}\s*[^\r\n]*\Z"

    @staticmethod
    def _ends_with_pager(output: str) -> bool:
        return _PAGER_END.search(output) is not None

    @staticmethod
    def _ends_with_confirmation(output: str) -> bool:
        return _CONFIRMATION_END.search(output) is not None

    @staticmethod
    def _ends_with_secret_prompt(output: str) -> bool:
        return _SECRET_END.search(output) is not None

    @staticmethod
    def _ends_with_prompt(session: _ManagedSession, output: str) -> bool:
        return re.search(SessionManager._prompt_end_pattern(session), output) is not None

    @staticmethod
    def _ends_with_help_prompt(session: _ManagedSession, output: str) -> bool:
        return re.search(SessionManager._help_prompt_tail_pattern(session), output) is not None

    @staticmethod
    def _strip_help_prompt(session: _ManagedSession, output: str) -> str:
        return re.sub(SessionManager._help_prompt_tail_pattern(session), "", output)

    @staticmethod
    def _strip_prompt(session: _ManagedSession, output: str) -> str:
        return re.sub(SessionManager._prompt_end_pattern(session), "", output)

    def _audit_interaction(
        self,
        session: _ManagedSession,
        event: str,
        outcome: str,
        **details: object,
    ) -> None:
        self._audit_event(
            event,
            target=session.target,
            platform=session.platform,
            outcome=outcome,
            redactor=session.redactor,
            session_id=session.session_id,
            **details,
        )

    def _transport_profile(
        self, target: Target
    ) -> DirectConnection | ProxyJumpConnection | NestedConnection | ConsoleConnection:
        profile = self._config.connections.connections[target.connection]
        if not isinstance(
            profile, (DirectConnection, ProxyJumpConnection, NestedConnection, ConsoleConnection)
        ):
            raise TransportError(
                f"connection profile {target.connection!r} is {profile.type!r}; "
                "only direct, proxyjump, nested and console profiles are supported"
            )
        if isinstance(profile, NestedConnection) and profile.next_protocol == "telnet":
            self._require_telnet_allowed(target)
        return profile

    def _resolve_credentials(self, name: str) -> Credentials:
        profile = self._config.credentials.credentials[name]
        return self._credentials.resolve(profile)

    def _open_jump_client(
        self, profile: ProxyJumpConnection, credentials: Credentials
    ) -> paramiko.SSHClient:
        """Authenticate the configured bastion after its key has been checked."""
        timeout = float(self._config.policy.runtime.command_timeout)
        client = self._jump_client_factory()
        try:
            client.load_host_keys(str(self._host_keys.path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            if credentials.key_file is not None:
                client.connect(
                    profile.jump_host,
                    port=profile.jump_port,
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
                    profile.jump_host,
                    port=profile.jump_port,
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
            timeout=float(self._config.policy.runtime.command_timeout),
        )
        if channel is None:
            raise TransportError("SSH jump host refused the target forwarding channel")
        return channel

    @staticmethod
    def _close_jump_client(jump_client: paramiko.SSHClient | None) -> None:
        if jump_client is not None:
            jump_client.close()

    def _connection_params(
        self,
        target: Target,
        platform: Platform,
        credentials: Credentials,
        port: int,
        *,
        sock: object | None = None,
        profile: ConnectionProfile | None = None,
    ) -> dict[str, object]:
        timeout = float(self._config.policy.runtime.command_timeout)
        params: dict[str, object] = {
            "device_type": platform.driver,
            "host": target.host,
            "port": port,
            "username": credentials.username,
            "conn_timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "fast_cli": False,
            "ssh_strict": True,
            "system_host_keys": False,
            "alt_host_keys": True,
            "alt_key_file": str(self._host_keys.path),
        }
        if isinstance(profile, DirectConnection):
            disabled = _disabled_algorithms(profile)
            if disabled is not None:
                params["disabled_algorithms"] = disabled
        if credentials.key_file is not None:
            params["use_keys"] = True
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

    def _telnet_connection_params(
        self, target: Target, platform: Platform, credentials: Credentials, port: int
    ) -> dict[str, object]:
        if platform.telnet_driver is None:
            raise TransportError(
                f"platform {platform.name!r} has no Telnet driver; "
                "set telnet_driver in connections.yml"
            )
        if credentials.password is None:
            raise TransportError("Telnet target credential has no password")
        timeout = float(self._config.policy.runtime.command_timeout)
        return {
            "device_type": platform.telnet_driver,
            "host": target.host,
            "port": port,
            "username": credentials.username,
            "password": credentials.password,
            "conn_timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "fast_cli": False,
        }

    def _require_telnet_allowed(self, target: Target) -> None:
        if not target.allow_telnet:
            raise TransportError(
                f"Telnet is not enabled for target {target.name!r}; "
                "set allow_telnet: true on the device"
            )
        if self._config.policy.defaults.telnet == "deny":
            raise TransportError("Telnet is denied by policy defaults")

    def _nested_connection_params(
        self, profile: NestedConnection, credentials: Credentials
    ) -> dict[str, object]:
        timeout = float(self._config.policy.runtime.command_timeout)
        params: dict[str, object] = {
            "device_type": "generic_termserver",
            "host": profile.host,
            "port": profile.port or 22,
            "username": credentials.username,
            "conn_timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "fast_cli": False,
            "ssh_strict": True,
            "system_host_keys": False,
            "alt_host_keys": True,
            "alt_key_file": str(self._host_keys.path),
        }
        if credentials.key_file is not None:
            params["use_keys"] = True
            params["key_file"] = credentials.key_file
            if credentials.key_passphrase is not None:
                params["passphrase"] = credentials.key_passphrase
        elif credentials.password is not None:
            params["password"] = credentials.password
        else:
            raise TransportError("nested intermediate credential has no password or SSH key")
        return params

    def _nested_login(
        self,
        connection: TerminalConnection,
        target: Target,
        credentials: Credentials,
        next_port: int,
    ) -> None:
        """Reach the final target from the intermediate shell with an ssh command."""
        timeout = float(self._config.policy.runtime.command_timeout)
        command = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"{credentials.username}@{target.host}"
        )
        if next_port != 22:
            command += f" -p {next_port}"
        connection.write_channel(command + "\n")
        connection.read_until_pattern(r"[Pp]assword\s*:", read_timeout=timeout)
        if credentials.password is None:
            raise TransportError("nested target credential has no password")
        connection.write_channel(credentials.password + "\n")

    def _nested_telnet_login(
        self,
        connection: TerminalConnection,
        target: Target,
        credentials: Credentials,
        next_port: int,
    ) -> None:
        """Reach the final target from the intermediate shell with a telnet command."""
        timeout = float(self._config.policy.runtime.command_timeout)
        command = f"telnet {target.host}"
        if next_port != 23:
            command += f" {next_port}"
        connection.write_channel(command + "\n")
        connection.read_until_pattern(
            r"(?:[Ll]ogin:|[Uu]sername:|[Pp]assword\s*:)", read_timeout=timeout
        )
        if credentials.password is None:
            raise TransportError("nested Telnet target credential has no password")
        connection.write_channel(credentials.username + "\n")
        connection.read_until_pattern(r"[Pp]assword\s*:", read_timeout=timeout)
        connection.write_channel(credentials.password + "\n")

    @staticmethod
    def _transport_warnings(profile: ConnectionProfile) -> list[str]:
        warnings: list[str] = []
        if isinstance(profile, DirectConnection) and profile.protocol == "legacy_ssh":
            warnings.append("legacy SSH profile in use")
        if isinstance(profile, DirectConnection) and (
            profile.host_key_algorithms or profile.kex_algorithms or profile.ciphers
        ):
            warnings.append("explicit legacy algorithm overrides in use")
        if isinstance(profile, DirectConnection) and profile.protocol == "telnet":
            warnings.append("Telnet transmits credentials and traffic in cleartext")
        if isinstance(profile, NestedConnection):
            if profile.next_protocol == "telnet":
                warnings.append("nested Telnet transmits credentials and traffic in cleartext")
            else:
                warnings.append("nested SSH uses the intermediate host's SSH client")
        if isinstance(profile, ConsoleConnection):
            warnings.append("console access transmits credentials and traffic in cleartext")
        return warnings

    @staticmethod
    def _route_details(profile: ConnectionProfile) -> dict[str, object]:
        if isinstance(profile, ProxyJumpConnection):
            return {
                "route": "proxyjump",
                "jump_host": profile.jump_host,
                "jump_port": profile.jump_port,
            }
        if isinstance(profile, NestedConnection):
            return {
                "route": "nested",
                "intermediate_host": profile.host,
                "intermediate_port": profile.port or 22,
            }
        if isinstance(profile, DirectConnection) and profile.protocol == "telnet":
            return {"route": "telnet"}
        if isinstance(profile, ConsoleConnection):
            return {"route": "console"}
        return {"route": "direct"}

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
                try:
                    session.connection.disconnect()
                finally:
                    self._close_jump_client(session.jump_client)
                    session.jump_client = None
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


def _visible_terminal_text(output: str) -> str:
    """Remove ANSI styling before matching a terminal marker at output end."""
    return _ANSI_ESCAPE.sub("", output).replace("\r", "").replace("\x08", "")


def _last_terminal_line(output: str) -> str:
    """Return the last non-empty terminal line for a pending prompt result."""
    for line in reversed(_visible_terminal_text(output).splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _allowed_responses(output: str) -> tuple[str, ...]:
    """Return the exact finite response set represented by a matched prompt."""
    normalized = output.lower()
    if "yes/no" in normalized:
        return ("yes", "no")
    return ("y", "n")
