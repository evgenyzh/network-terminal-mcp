"""Change plan storage and lifecycle transitions.

The manager stores plans server-side so ``apply_change`` executes exactly the
canonical command list captured at ``plan_change`` time. It validates state
transitions but does not execute commands itself; session command execution
lives in the session manager.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import UTC, datetime

from network_terminal_mcp.changes.models import (
    ChangeCommand,
    ChangePlan,
    ChangeState,
)
from network_terminal_mcp.errors import ChangeError


def plan_hash(commands: list[str]) -> str:
    """Return a stable hash of the canonical command list."""
    digest = hashlib.sha256()
    for command in commands:
        digest.update(command.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class ChangeManager:
    """Own change plans and enforce their one-shot lifecycle."""

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
        target: str,
        host: str,
        title: str,
        commands: list[str],
        auto_approve: bool,
    ) -> ChangePlan:
        plan = ChangePlan(
            change_id=uuid.uuid4().hex,
            session_id=session_id,
            target=target,
            host=host,
            title=title,
            commands=[ChangeCommand(command=command) for command in commands],
            hash=plan_hash(commands),
            auto_approve=auto_approve,
            created_at=self._now(),
        )
        with self._lock:
            self._plans[plan.change_id] = plan
        return plan

    def get(self, change_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(change_id)
        if plan is None:
            raise ChangeError(f"unknown or expired change plan {change_id}")
        return plan

    def confirm(self, change_id: str) -> ChangePlan:
        """Transition a proposed plan to confirmed (no execution)."""
        with self._lock:
            plan = self._plans.get(change_id)
            if plan is None:
                raise ChangeError(f"unknown or expired change plan {change_id}")
            if plan.state != ChangeState.PROPOSED:
                raise ChangeError(
                    f"change plan {change_id} cannot be confirmed from "
                    f"{plan.state.value}"
                )
            plan = plan.model_copy(update={"state": ChangeState.CONFIRMED})
            self._plans[change_id] = plan
        return plan

    def mark_applied(self, change_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(change_id)
            if plan is None:
                raise ChangeError(f"unknown or expired change plan {change_id}")
            plan = plan.model_copy(
                update={
                    "state": ChangeState.APPLIED,
                    "applied_at": self._now(),
                }
            )
            self._plans[change_id] = plan
        return plan

    def mark_failed(self, change_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(change_id)
            if plan is None:
                raise ChangeError(f"unknown or expired change plan {change_id}")
            plan = plan.model_copy(update={"state": ChangeState.FAILED})
            self._plans[change_id] = plan
        return plan

    def abort(self, change_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(change_id)
            if plan is None:
                raise ChangeError(f"unknown or expired change plan {change_id}")
            if plan.state not in (ChangeState.PROPOSED, ChangeState.CONFIRMED):
                raise ChangeError(
                    f"change plan {change_id} cannot be aborted from "
                    f"{plan.state.value}"
                )
            plan = plan.model_copy(update={"state": ChangeState.ABORTED})
            self._plans[change_id] = plan
        return plan

    def finalize(self, change_id: str) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(change_id)
            if plan is None:
                raise ChangeError(f"unknown or expired change plan {change_id}")
            if plan.state != ChangeState.APPLIED:
                raise ChangeError(
                    f"change plan {change_id} cannot be finalized from "
                    f"{plan.state.value}"
                )
            plan = plan.model_copy(
                update={
                    "state": ChangeState.FINALIZED,
                    "finalized_at": self._now(),
                }
            )
            self._plans[change_id] = plan
        return plan
