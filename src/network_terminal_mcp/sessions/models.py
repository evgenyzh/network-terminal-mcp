"""Data models for managed terminal sessions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from network_terminal_mcp.config.models import Action


class SessionState(StrEnum):
    CONNECTING = "connecting"
    READY = "ready"
    PAGING = "paging"
    AWAITING_RESPONSE = "awaiting_response"
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


class TerminalOutput(BaseModel):
    """Output and pending terminal interaction metadata."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    output: str = ""
    truncated: bool = False
    output_offset: int | None = None
    next_output_offset: int | None = None
    pager_active: bool = False
    response_required: bool = False
    device_prompt: str | None = None
    allowed_responses: list[str] = Field(default_factory=list)


class CommandResult(TerminalOutput):
    """Result of an attempted single CLI command."""

    command: str
    policy: Action
    executed: bool
    confirmation_required: bool = False


class CliHelpResult(TerminalOutput):
    """Result of a non-executing CLI help request."""

    line: str
    policy: Action
    executed: bool
    confirmation_required: bool = False


class ControlResult(TerminalOutput):
    """Result of a state-bound terminal control action."""

    action: Literal["space", "q", "ctrl-c"]


class ResponseResult(TerminalOutput):
    """Result of an allowlisted response to a device confirmation prompt."""

    response: str


class OutputChunk(BaseModel):
    """A bounded slice of a session's accumulated output."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    offset: int
    output: str
    next_offset: int | None = None
    oldest_offset: int = 0
    truncated: bool = False
