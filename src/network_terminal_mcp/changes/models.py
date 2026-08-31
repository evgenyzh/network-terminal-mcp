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


class SafetyNet(BaseModel):
    """Optional reload/commit rollback safety net declared by the model.

    ``save`` snapshots the pre-change config to the device's startup config
    before any change command runs, ``arm`` schedules the reload/commit, and
    ``cancel`` is sent on finalize. All are opaque vendor command lines written
    by the model; the server executes them verbatim and never inspects them.
    """

    save: str
    arm: str
    cancel: str


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
    platform: str
    title: str
    commands: list[ChangeCommand]
    hash: str
    safety_net: SafetyNet | None = None
    auto_approve: bool = False
    state: ChangeState = ChangeState.PROPOSED
    reboot_cancel_required: bool = False
    created_at: datetime
    applied_at: datetime | None = None
    finalized_at: datetime | None = None


class ChangeResult(BaseModel):
    """Serializable change operation result returned by MCP tools."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    session_id: str
    target: str
    platform: str
    title: str
    state: ChangeState
    hash: str
    commands: list[dict[str, object]] = Field(default_factory=list)
    safety_net: SafetyNet | None = None
    auto_approve: bool = False
    confirmation_required: bool = False
    reboot_cancel_required: bool = False
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
            platform=plan.platform,
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
            safety_net=plan.safety_net,
            auto_approve=plan.auto_approve,
            confirmation_required=confirmation_required,
            reboot_cancel_required=plan.reboot_cancel_required,
            output=output,
            warnings=warnings or [],
            error=error,
        )


ChangeLiteral = Literal[
    "proposed", "confirmed", "applied", "finalized", "aborted", "failed"
]
