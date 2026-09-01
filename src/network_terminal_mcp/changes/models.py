"""Data models for configuration change plans."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChangeState(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    APPLIED = "applied"
    FINALIZED = "finalized"
    ABORTED = "aborted"
    FAILED = "failed"


class ChangeCommand(BaseModel):
    """One planned command and its per-command execution outcome."""

    command: str
    executed: bool = False
    error: str | None = None


class ChangePlan(BaseModel):
    """A configuration change plan with its lifecycle state."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    session_id: str
    target: str
    host: str
    title: str
    commands: list[ChangeCommand]
    hash: str
    auto_approve: bool = False
    state: ChangeState = ChangeState.PROPOSED
    created_at: datetime
    applied_at: datetime | None = None
    finalized_at: datetime | None = None


class ChangeResult(BaseModel):
    """Serializable change operation result returned by MCP tools."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    session_id: str
    target: str
    title: str
    state: ChangeState
    hash: str
    commands: list[dict[str, object]] = Field(default_factory=list)
    auto_approve: bool = False
    confirmation_required: bool = False
    output: str = ""
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_plan(
        cls,
        plan: ChangePlan,
        *,
        confirmation_required: bool = False,
        output: str = "",
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> ChangeResult:
        return cls(
            change_id=plan.change_id,
            session_id=plan.session_id,
            target=plan.target,
            title=plan.title,
            state=plan.state,
            hash=plan.hash,
            commands=[
                {
                    "command": command.command,
                    "executed": command.executed,
                    "error": command.error,
                }
                for command in plan.commands
            ],
            auto_approve=plan.auto_approve,
            confirmation_required=confirmation_required,
            output=output,
            warnings=warnings or [],
            error=error,
        )


ChangeLiteral = Literal[
    "proposed", "confirmed", "applied", "finalized", "aborted", "failed"
]
