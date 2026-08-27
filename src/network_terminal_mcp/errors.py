"""Structured exception hierarchy for network-terminal-mcp."""

from __future__ import annotations

from typing import NoReturn


class NetworkMCPError(Exception):
    """Base class for all project errors.

    Every error carries a stable machine-readable code and a human-readable
    message. Secrets must never be embedded in either field; use the
    :mod:`network_terminal_mcp.redaction` helpers before raising.
    """

    code: str = "network_mcp_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        if code is not None:
            self.code = code
        super().__init__(f"{self.code}: {message}")

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {"code": self.code, "message": self.message}


class ConfigError(NetworkMCPError):
    """Configuration is missing, malformed, or fails schema validation."""

    code = "config_error"


class CredentialError(NetworkMCPError):
    """A credential profile or the underlying secret backend failed."""

    code = "credential_error"


class PolicyError(NetworkMCPError):
    """A command or action was denied or is not covered by policy."""

    code = "policy_error"


class SessionError(NetworkMCPError):
    """A terminal session is missing, busy, expired, or has failed."""

    code = "session_error"


class AuditError(NetworkMCPError):
    """Audit logging failed and the operation was aborted (fail-closed)."""

    code = "audit_error"


class TransportError(NetworkMCPError):
    """Connection, terminal server hop, or device interaction failed."""

    code = "transport_error"


class TargetError(NetworkMCPError):
    """A device target is unknown or cannot be resolved."""

    code = "target_error"


def fail_closed_audit(message: str) -> NoReturn:
    """Raise the audit error that aborts an operation when logging fails."""
    raise AuditError(message)
