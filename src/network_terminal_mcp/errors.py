"""Expected error hierarchy for network-terminal-mcp.

Only errors derived from :class:`NetworkMCPError` are converted to MCP
``ToolError`` at the server boundary. Anything else is an internal bug and is
deliberately left to the SDK's crash handling.
"""

from __future__ import annotations

from typing import NoReturn


class NetworkMCPError(Exception):
    """Base class for expected, model-readable errors.

    Every error carries a stable machine-readable code and a human-readable
    message. Secrets must never be embedded in either field; use the session
    redactor before raising.
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


class CommandError(NetworkMCPError):
    """A connection command is empty, unsupported, or malformed."""

    code = "command_error"


class CredentialError(NetworkMCPError):
    """A secret source is missing, unreadable, or empty."""

    code = "credential_error"


class SessionError(NetworkMCPError):
    """A terminal session is missing, closed, or otherwise unusable."""

    code = "session_error"


class AuditError(NetworkMCPError):
    """Audit logging failed and the operation was aborted (fail-closed)."""

    code = "audit_error"


class TransportError(NetworkMCPError):
    """A client process, PTY, serial device, or terminal I/O failed."""

    code = "transport_error"


def fail_closed_audit(message: str) -> NoReturn:
    """Raise the audit error that aborts an operation when logging fails."""
    raise AuditError(message)
