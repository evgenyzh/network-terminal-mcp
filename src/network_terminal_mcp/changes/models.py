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
    ABORTED = "aborted"
    FAILED = "failed"


class ChangeCommand(BaseModel):
    """One planned command and its per-command execution outcome."""

    command: str
    executed: bool = False
    error: str | None = None


class ChangePlan(BaseModel):
    """A configuration change plan keyed by its session.

    At most one pending plan exists per session; the plan is stored server-side
    so ``apply_change`` executes exactly the canonical command list captured at
    ``plan_change`` time.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    commands: list[ChangeCommand]
    auto_approve: bool = False
    state: ChangeState = ChangeState.PROPOSED
    created_at: datetime
    applied_at: datetime | None = None


class ChangeResult(BaseModel):
    """Serializable change operation result returned by MCP tools."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    state: ChangeState
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
            session_id=plan.session_id,
            state=plan.state,
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
    "proposed", "confirmed", "applied", "aborted", "failed"
]
