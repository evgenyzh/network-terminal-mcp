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
    SafetyNet,
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
        safety_net: SafetyNet | None,
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
            safety_net=safety_net,
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
                    "reboot_cancel_required": False,
                }
            )
            self._plans[change_id] = plan
        return plan

    def set_reboot_required(self, change_id: str, required: bool) -> ChangePlan:
        with self._lock:
            plan = self._plans.get(change_id)
            if plan is None:
                raise ChangeError(f"unknown or expired change plan {change_id}")
            plan = plan.model_copy(update={"reboot_cancel_required": required})
            self._plans[change_id] = plan
        return plan

    def plans_for_session(self, session_id: str) -> list[ChangePlan]:
        with self._lock:
            return [
                plan
                for plan in self._plans.values()
                if plan.session_id == session_id
            ]

    def has_active_reboot(self, session_id: str) -> ChangePlan | None:
        """Return a plan that still requires the scheduled reboot to be cancelled."""
        with self._lock:
            for plan in self._plans.values():
                if (
                    plan.session_id == session_id
                    and plan.reboot_cancel_required
                    and plan.state in (ChangeState.CONFIRMED, ChangeState.APPLIED)
                ):
                    return plan
        return None
