"""Data models for managed terminal sessions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from network_terminal_mcp.config.models import Action


class SessionState(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


class SessionInfo(BaseModel):
    """Serializable session metadata returned by MCP tools."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    target: str
    host: str
    platform: str
    dialect: str
    prompt: str
    state: SessionState
    created_at: datetime
    last_used_at: datetime
    warnings: list[str] = Field(default_factory=list)


class CommandResult(BaseModel):
    """Result of an attempted single CLI command."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    command: str
    policy: Action
    executed: bool
    output: str = ""
    truncated: bool = False
    confirmation_required: bool = False
    output_offset: int | None = None
    next_output_offset: int | None = None


class OutputChunk(BaseModel):
    """A bounded slice of a session's accumulated output."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    offset: int
    output: str
    next_offset: int | None = None
    oldest_offset: int = 0
    truncated: bool = False
