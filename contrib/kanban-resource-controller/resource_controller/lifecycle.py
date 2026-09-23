from __future__ import annotations

from .policy import AdmissionPolicy


class LifecycleHold(RuntimeError):
    pass


class LifecycleGuard:
    """Admission-only lifecycle guard; it has no ESTOP or signal operations."""

    estop_action = None

    def __init__(self, policy: AdmissionPolicy) -> None:
        self.policy = policy
        self.manual_hold = False
        self.hold_reason: str | None = None
        self.stop_requested = False

    def hold(self, reason: str) -> None:
        if not reason:
            raise ValueError("hold reason is required")
        self.manual_hold = True
        self.hold_reason = reason

    def resume(self, *, now: float) -> None:
        self.manual_hold = False
        self.hold_reason = None
        self.policy.command_consumed(now)

    def request_stop(
        self,
        *,
        command_running: bool,
        workers: int,
        descendants_known: bool,
    ) -> None:
        self.hold("stop-requested")
        if command_running:
            raise LifecycleHold("command is still running")
        if workers:
            raise LifecycleHold("worker descendants have not drained")
        if not descendants_known:
            raise LifecycleHold("descendant or launchd coalition identity is unknown")
        self.stop_requested = True
