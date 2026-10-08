"""Data models for managed raw terminal sessions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SessionState(StrEnum):
    READY = "ready"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


class SessionInfo(BaseModel):
    """Serializable session metadata returned by MCP tools."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    route: str
    host: str
    prompt: str = ""
    state: SessionState
    created_at: datetime
    last_used_at: datetime
    warnings: list[str] = Field(default_factory=list)


class TerminalOutput(BaseModel):
    """A bounded slice of freshly read terminal output."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    output: str = ""
    truncated: bool = False
    output_offset: int | None = None
    next_output_offset: int | None = None


class TerminalWriteResult(BaseModel):
    """Result of writing raw input to a session.

    The written text is recorded in the audit but deliberately not echoed
    back to the model, so repetitive input does not inflate the context.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    bytes_sent: int
    state: SessionState


class TerminalSecretResult(BaseModel):
    """Result of writing a pass-referenced secret; never contains the secret."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    entry: str
    bytes_sent: int
    state: SessionState


class OutputChunk(BaseModel):
    """A bounded slice of a session's accumulated output."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    offset: int
    output: str
    next_offset: int | None = None
    oldest_offset: int = 0
    truncated: bool = False
