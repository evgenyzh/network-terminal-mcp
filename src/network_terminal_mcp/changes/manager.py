"""Change plan storage and lifecycle transitions.

The manager stores at most one plan per session so ``apply_change`` executes
exactly the canonical command list captured at ``plan_change`` time. It
validates state transitions but does not execute commands itself; session
command execution lives in the session manager.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from network_terminal_mcp.changes.models import (
    ChangeCommand,
    ChangePlan,
    ChangeState,
)
from network_terminal_mcp.errors import ChangeError


class ChangeManager:
    """Own change plans per session and enforce their one-shot lifecycle."""

    def __init__(self, *, clock: object | None = None) -> None:
        self._plans: dict[str, ChangePlan] = {}
        self._lock = threading.RLock()
        self._clock = clock

    def _now(self) -> datetime:
        if callable(self._clock):
            return datetime.fromtimestamp(float(self._clock()), tz=UTC)
        return datetime.now(UTC)

    def create(
        self,
        *,
        session_id: str,
        commands: list[str],
        auto_approve: bool,
    ) -> ChangePlan:
        plan = ChangePlan(
            session_id=session_id,
            commands=[ChangeCommand(command=command) for command in commands],
            auto_approve=auto_approve,
            created_at=self._now(),
        )
        with self._lock:
            self._plans[session_id] = plan
        return plan

    def get(self, session_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(session_id)
        if plan is None:
            raise ChangeError(
                f"no pending change plan for session {session_id}"
            )
        return plan

    def confirm(self, session_id: str) -> ChangePlan:
        """Transition a proposed plan to confirmed (no execution)."""
        with self._lock:
            plan = self._plans.get(session_id)
            if plan is None:
                raise ChangeError(
                    f"no pending change plan for session {session_id}"
                )
            if plan.state != ChangeState.PROPOSED:
                raise ChangeError(
                    f"change plan for session {session_id} cannot be "
                    f"confirmed from {plan.state.value}"
                )
            plan = plan.model_copy(update={"state": ChangeState.CONFIRMED})
            self._plans[session_id] = plan
        return plan

    def mark_applied(self, session_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(session_id)
            if plan is None:
                raise ChangeError(
                    f"no pending change plan for session {session_id}"
                )
            plan = plan.model_copy(
                update={
                    "state": ChangeState.APPLIED,
                    "applied_at": self._now(),
                }
            )
            self._plans[session_id] = plan
        return plan

    def mark_failed(self, session_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(session_id)
            if plan is None:
                raise ChangeError(
                    f"no pending change plan for session {session_id}"
                )
            plan = plan.model_copy(update={"state": ChangeState.FAILED})
            self._plans[session_id] = plan
        return plan

    def abort(self, session_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(session_id)
            if plan is None:
                raise ChangeError(
                    f"no pending change plan for session {session_id}"
                )
            if plan.state not in (ChangeState.PROPOSED, ChangeState.CONFIRMED):
                raise ChangeError(
                    f"change plan for session {session_id} cannot be aborted "
                    f"from {plan.state.value}"
                )
            plan = plan.model_copy(update={"state": ChangeState.ABORTED})
            self._plans[session_id] = plan
        return plan
