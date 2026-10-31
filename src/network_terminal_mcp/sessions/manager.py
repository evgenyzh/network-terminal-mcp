"""Session manager for command-open persistent terminals.

A session is one raw terminal stream. The manager owns lifecycle, bounded
output, the read cursor, redaction, and audit; it never interprets device
commands or known prompt shapes.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Protocol

from network_terminal_mcp.audit import AuditLogger
from network_terminal_mcp.commands import ParsedCommand, parse_command
from network_terminal_mcp.errors import (
    CredentialError,
    NetworkMCPError,
    SessionError,
    TransportError,
)
from network_terminal_mcp.redaction import Redactor
from network_terminal_mcp.sessions.models import (
    OutputChunk,
    SecretSource,
    SessionInfo,
    SessionState,
    TerminalOutput,
    TerminalSecretResult,
    TerminalWriteResult,
)
from network_terminal_mcp.terminal import (
    PtyTerminal,
    SerialTerminal,
    TerminalConnection,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEFAULT_AUDIT_FILE = Path("network-terminal-mcp/audit.jsonl")


class AuditSink(Protocol):
    def write(self, record: dict[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Built-in runtime defaults; no local configuration file is required."""

    io_timeout: float = 60.0
    max_read_timeout: float = 300.0
    write_timeout: float = 10.0
    max_write_bytes: int = 65_536
    max_inline_output_bytes: int = 65_536
    max_session_buffer_bytes: int = 1_048_576
    max_open_sessions: int = 10
    read_quiet_seconds: float = 0.5
    startup_timeout: float = 1.0
    startup_quiet_seconds: float = 0.2
    startup_output_tail_bytes: int = 8_192


TerminalFactory = Callable[[ParsedCommand], TerminalConnection]


def default_audit_path() -> Path:
    """Return ``$XDG_STATE_HOME/network-terminal-mcp/audit.jsonl``."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local/state"
    return base / _DEFAULT_AUDIT_FILE


def _terminal_factory_default(
    parsed: ParsedCommand, *, write_timeout: float = 10.0
) -> TerminalConnection:
    if parsed.is_serial:
        assert parsed.device is not None
        assert parsed.baudrate is not None
        serial_terminal = SerialTerminal()
        serial_terminal.open(device=parsed.device, baudrate=parsed.baudrate)
        return serial_terminal
    pty_terminal = PtyTerminal(write_timeout=write_timeout)
    pty_terminal.start(parsed.argv)
    return pty_terminal


@dataclass
class _ManagedSession:
    session_id: str
    parsed: ParsedCommand
    connection: TerminalConnection
    redactor: Redactor
    warnings: list[str]
    created_at: datetime
    last_used_at: datetime
    last_output_at: datetime | None = None
    state: SessionState = SessionState.READY
    output: str = ""
    output_start: int = 0
    output_truncated: bool = False
    read_cursor: int = 0
    exit_code: int | None = None
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionManager:
    """Own persistent terminals and expose synchronous raw I/O operations."""

    def __init__(
        self,
        *,
        limits: RuntimeLimits | None = None,
        terminal_factory: TerminalFactory = _terminal_factory_default,
        audit_logger: AuditSink | None = None,
    ) -> None:
        self._limits = limits or RuntimeLimits()
        self._terminal_factory: TerminalFactory = (
            terminal_factory
            if terminal_factory is not None
            else partial(
                _terminal_factory_default,
                write_timeout=self._limits.write_timeout,
            )
        )
        self._audit: AuditSink = audit_logger or AuditLogger(default_audit_path())
        self._sessions: dict[str, _ManagedSession] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def open_session(self, command: str) -> SessionInfo:
        """Open one persistent terminal from a connection command."""
        parsed = parse_command(command)
        with self._lock:
            if self._active_count() >= self._limits.max_open_sessions:
                raise SessionError(
                    "maximum number of open sessions reached "
                    f"({self._limits.max_open_sessions}); close one first"
                )

        redactor = Redactor()
        self._audit_event(
            "open_session",
            parsed=parsed,
            outcome="started",
            redactor=redactor,
        )
        connection: TerminalConnection | None = None
        exit_code: int | None = None
        try:
            connection = self._terminal_factory(parsed)
            startup_output = _normalize_output(
                connection.read_channel_timing(
                    last_read=self._limits.startup_quiet_seconds,
                    read_timeout=self._limits.startup_timeout,
                )
            )
            if not connection.is_alive():
                exit_code = connection.exit_code
                message = _startup_failure(parsed, exit_code, startup_output, self._limits)
                raise TransportError(message)
        except Exception as exc:
            if connection is not None:
                connection.disconnect()
            if isinstance(exc, NetworkMCPError):
                message = redactor.redact(str(exc))
                self._audit_event(
                    "open_session",
                    parsed=parsed,
                    outcome="failed",
                    redactor=redactor,
                    exit_code=exit_code,
                    error=message,
                )
                raise
            raise

        assert connection is not None
        now = datetime.now(UTC)
        warnings: list[str] = []
        if not startup_output:
            warnings.append(
                "no initial output; use terminal_read to inspect the session"
            )
        session = _ManagedSession(
            session_id=uuid.uuid4().hex,
            parsed=parsed,
            connection=connection,
            redactor=redactor,
            warnings=warnings,
            created_at=now,
            last_used_at=now,
        )
        if startup_output:
            self._append_output(session, startup_output)
        with self._lock:
            if self._active_count() >= self._limits.max_open_sessions:
                connection.disconnect()
                self._audit_event(
                    "open_session",
                    parsed=parsed,
                    outcome="failed",
                    redactor=redactor,
                    error="maximum number of open sessions reached",
                )
                raise SessionError(
                    "maximum number of open sessions reached "
                    f"({self._limits.max_open_sessions}); close one first"
                )
            self._sessions[session.session_id] = session

        self._audit_event(
            "open_session",
            parsed=parsed,
            outcome="completed",
            redactor=redactor,
            session_id=session.session_id,
            warnings=warnings,
        )
        return self._session_info(session)

    def close_session(self, session_id: str) -> SessionInfo:
        """Close a session and retain its final status for inspection."""
        session = self._get_session(session_id)
        with session.lock:
            if session.state == SessionState.CLOSED:
                return self._session_info(session)
            self._audit_interaction(session, "close_session", "started")
            session.state = SessionState.CLOSING
            failure: str | None = None
            try:
                session.connection.disconnect()
            except Exception as exc:
                failure = session.redactor.redact(str(exc))
            finally:
                session.exit_code = session.connection.exit_code
                session.state = SessionState.CLOSED
                if failure is not None:
                    session.error = failure
                self._touch(session)
            if failure is not None:
                self._audit_interaction(
                    session, "close_session", "failed", error=failure
                )
                raise TransportError(
                    f"session {session_id} closed with an error: {failure}"
                )
            self._audit_interaction(
                session,
                "close_session",
                "completed",
                exit_code=session.exit_code,
            )
            return self._session_info(session)

    def session_status(self, session_id: str) -> SessionInfo:
        """Return current metadata for a session."""
        session = self._get_session(session_id)
        with session.lock:
            self._refresh_state(session)
            return self._session_info(session)

    def shutdown(self) -> None:
        """Close every child terminal; best effort during server shutdown."""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            with session.lock:
                if session.state == SessionState.CLOSED:
                    continue
                session.state = SessionState.CLOSING
                try:
                    session.connection.disconnect()
                except Exception:
                    pass
                session.exit_code = session.connection.exit_code
                session.state = SessionState.CLOSED
                self._touch(session)

    # ------------------------------------------------------------------
    # Raw terminal I/O
    # ------------------------------------------------------------------

    def terminal_write(
        self, session_id: str, data: str, *, enter: bool = True
    ) -> TerminalWriteResult:
        """Write exact input to the terminal stream.

        The whole ``data`` string is recorded in the audit after known-secret
        redaction. Passwords and other secrets must be sent with
        :meth:`terminal_write_secret` instead.
        """
        session = self._get_session(session_id)
        with session.lock:
            self._refresh_state(session)
            self._require_ready(session)
            payload = data + (self._enter_sequence(session) if enter else "")
            encoded = payload.encode("utf-8")
            if len(encoded) > self._limits.max_write_bytes:
                raise SessionError(
                    f"input exceeds max_write_bytes ({self._limits.max_write_bytes})"
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
                message = session.redactor.redact(str(exc))
                self._fail_session(session, message)
                self._audit_interaction(
                    session, "terminal_write", "failed", error=message
                )
                raise TransportError(
                    f"terminal write failed in session {session_id}: {message}"
                ) from exc
            self._refresh_state(session)
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
        self,
        session_id: str,
        *,
        value: str | None = None,
        env: str | None = None,
        file: str | None = None,
    ) -> TerminalSecretResult:
        """Send one secret from exactly one source and register it for redaction.

        The value is never returned and never written to audit; only the source
        kind and byte count are recorded.
        """
        session = self._get_session(session_id)
        with session.lock:
            self._refresh_state(session)
            self._require_ready(session)
            secret, source = _resolve_secret(value=value, env=env, file=file)
            payload = secret + self._enter_sequence(session)
            encoded = payload.encode("utf-8")
            if len(encoded) > self._limits.max_write_bytes:
                raise SessionError(
                    f"secret input exceeds max_write_bytes "
                    f"({self._limits.max_write_bytes})"
                )
            self._audit_interaction(
                session,
                "terminal_write_secret",
                "started",
                source=source,
                bytes=len(encoded),
            )
            session.redactor.add(secret)
            try:
                session.connection.write_channel(payload)
            except Exception as exc:
                message = session.redactor.redact(str(exc))
                self._fail_session(session, message)
                self._audit_interaction(
                    session,
                    "terminal_write_secret",
                    "failed",
                    source=source,
                    error=message,
                )
                raise TransportError(
                    f"secret write failed in session {session_id}: {message}"
                ) from exc
            self._refresh_state(session)
            self._touch(session)
            self._audit_interaction(
                session,
                "terminal_write_secret",
                "completed",
                source=source,
                bytes=len(encoded),
            )
            return TerminalSecretResult(
                session_id=session_id,
                source=source,
                bytes_sent=len(encoded),
                state=session.state,
            )

    def terminal_read(
        self, session_id: str, *, timeout: float | None = None
    ) -> TerminalOutput:
        """Read output until the stream is quiet or ``timeout`` expires."""
        session = self._get_session(session_id)
        with session.lock:
            if session.state == SessionState.CLOSING:
                raise SessionError(f"session {session_id} is closing")
            read_timeout = float(
                timeout if timeout is not None else self._limits.io_timeout
            )
            if read_timeout <= 0:
                raise SessionError("read timeout must be positive")
            maximum = float(self._limits.max_read_timeout)
            if read_timeout > maximum:
                raise SessionError(
                    f"read timeout exceeds max_read_timeout ({maximum:g})"
                )
            self._audit_interaction(
                session, "terminal_read", "started", timeout=read_timeout
            )
            # A startup banner (or the tail of a truncated previous read) is
            # already unread in the bounded buffer. Return it immediately
            # instead of blocking for more output that may never arrive.
            pending = session.read_cursor < session.output_start + len(session.output)
            output = ""
            if not pending:
                try:
                    raw = session.connection.read_channel_timing(
                        last_read=self._limits.read_quiet_seconds,
                        read_timeout=read_timeout,
                    )
                except Exception as exc:
                    message = session.redactor.redact(str(exc))
                    self._fail_session(session, message)
                    self._audit_interaction(
                        session, "terminal_read", "failed", error=message
                    )
                    raise TransportError(
                        f"terminal read failed in session {session_id}: {message}"
                    ) from exc
                output = session.redactor.redact(_normalize_output(raw))
                self._append_output(session, output)
            start = max(session.read_cursor, session.output_start)
            fresh = session.output[start - session.output_start :]
            inline, truncated = _truncate_utf8(
                fresh, self._limits.max_inline_output_bytes
            )
            if truncated:
                session.read_cursor = start + len(inline)
                next_offset: int | None = session.read_cursor
            else:
                session.read_cursor = start + len(fresh)
                next_offset = None
            self._refresh_state(session)
            self._touch(session)
            self._audit_interaction(
                session,
                "terminal_read",
                "completed",
                bytes=len(output.encode()),
                exit_code=session.exit_code,
            )
            return TerminalOutput(
                session_id=session_id,
                output=inline,
                truncated=truncated,
                output_offset=start,
                next_output_offset=next_offset,
            )

    def read_output(
        self, session_id: str, *, offset: int | None = None, limit: int | None = None
    ) -> OutputChunk:
        """Read accumulated, redacted output starting at the read cursor."""
        if offset is not None and offset < 0:
            raise SessionError("output offset must be non-negative")
        if limit is None:
            limit = self._limits.max_inline_output_bytes
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
            self._refresh_state(session)
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

    def _active_count(self) -> int:
        count = 0
        for session in self._sessions.values():
            # Reap a naturally exited process without waiting behind a
            # concurrent long read. A busy session still counts as active.
            if session.lock.acquire(blocking=False):
                try:
                    self._refresh_state(session)
                finally:
                    session.lock.release()
            if session.state in (SessionState.READY, SessionState.CLOSING):
                count += 1
        return count

    @staticmethod
    def _enter_sequence(session: _ManagedSession) -> str:
        return "\r" if session.parsed.is_serial else "\n"

    def _get_session(self, session_id: str) -> _ManagedSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"unknown session {session_id}")
        return session

    def _refresh_state(self, session: _ManagedSession) -> None:
        if session.state in (SessionState.CLOSED, SessionState.CLOSING):
            return
        if session.connection.is_alive():
            return
        session.exit_code = session.connection.exit_code
        if session.exit_code in (None, 0):
            session.state = SessionState.CLOSED
        else:
            session.state = SessionState.FAILED
            session.error = f"process exited with code {session.exit_code}"

    @staticmethod
    def _require_ready(session: _ManagedSession) -> None:
        if session.state != SessionState.READY:
            detail = f": {session.error}" if session.error else ""
            raise SessionError(
                f"session {session.session_id} is not ready: "
                f"{session.state.value}{detail}"
            )

    @staticmethod
    def _fail_session(session: _ManagedSession, message: str) -> None:
        session.state = SessionState.FAILED
        session.error = message
        session.exit_code = session.connection.exit_code

    def _append_output(self, session: _ManagedSession, output: str) -> tuple[int, str]:
        if output:
            session.last_output_at = datetime.now(UTC)
        previous_end = session.output_start + len(session.output)
        combined = session.output + output
        trimmed, dropped = _keep_utf8_tail(
            combined, self._limits.max_session_buffer_bytes
        )
        if dropped:
            session.output_start += dropped
            session.output_truncated = True
        session.output = trimmed
        start = max(previous_end, session.output_start)
        session.read_cursor = max(session.read_cursor, session.output_start)
        return start, session.output[start - session.output_start :]

    @staticmethod
    def _touch(session: _ManagedSession) -> None:
        session.last_used_at = datetime.now(UTC)

    def _session_info(self, session: _ManagedSession) -> SessionInfo:
        now = datetime.now(UTC)
        idle_since = session.last_output_at or session.created_at
        idle_seconds = max(0.0, (now - idle_since).total_seconds())
        return SessionInfo(
            session_id=session.session_id,
            command=session.parsed.command,
            state=session.state,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            last_output_at=session.last_output_at,
            idle_seconds=round(idle_seconds, 3),
            warnings=list(session.warnings),
            exit_code=session.exit_code,
            error=session.redactor.redact(session.error) if session.error else None,
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
            parsed=session.parsed,
            outcome=outcome,
            redactor=session.redactor,
            session_id=session.session_id,
            **details,
        )

    def _audit_event(
        self,
        event: str,
        *,
        parsed: ParsedCommand,
        outcome: str,
        redactor: Redactor,
        **details: object,
    ) -> None:
        record: dict[str, object] = {
            "event": event,
            "outcome": outcome,
            "command": parsed.command,
            "protocol": parsed.kind,
        }
        record.update(details)
        self._audit.write(_redact_record(record, redactor))


def _resolve_secret(
    *, value: str | None, env: str | None, file: str | None
) -> tuple[str, SecretSource]:
    provided = sum(item is not None for item in (value, env, file))
    if provided != 1:
        raise CredentialError("exactly one of value, env, or file is required")
    source: SecretSource
    if value is not None:
        secret, source = value, "value"
    elif env is not None:
        if not env:
            raise CredentialError("environment variable name is empty")
        if env not in os.environ:
            raise CredentialError(f"environment variable {env!r} is not set")
        secret, source = os.environ[env], "env"
    else:
        assert file is not None
        path = Path(file).expanduser()
        try:
            secret = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CredentialError(
                f"secret file is not readable: {path}: {exc}"
            ) from exc
        if secret.endswith("\r\n"):
            secret = secret[:-2]
        elif secret.endswith(("\n", "\r")):
            secret = secret[:-1]
        source = "file"
    if not secret.strip():
        raise CredentialError(f"{source} secret is empty")
    return secret, source


def _startup_failure(
    parsed: ParsedCommand,
    exit_code: int | None,
    output: str,
    limits: RuntimeLimits,
) -> str:
    if exit_code is None:
        headline = f"{parsed.kind} exited during startup"
    else:
        headline = f"{parsed.kind} exited with code {exit_code} during startup"
    tail, _ = _keep_utf8_tail(output.strip(), limits.startup_output_tail_bytes)
    if not tail:
        return f"{headline} without output"
    return f"{headline}:\n{tail}"


def _normalize_output(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


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
