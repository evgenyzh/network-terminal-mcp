"""Data models for configuration change execution results."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChangeCommand(BaseModel):
    """One change command and its per-command execution outcome."""

    model_config = ConfigDict(frozen=True)

    command: str
    executed: bool = False
    error: str | None = None


class ChangeResult(BaseModel):
    """Serializable change execution result returned by MCP tools."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    commands: list[ChangeCommand] = Field(default_factory=list)
    output: str = ""
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
