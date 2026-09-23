from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from .config import ControllerConfig
from .inventory import LiveWorker, admission_capacity
from .policy import AdmissionPolicy, HostSample
from .priority import PredictedPick, select_pick
from .storage import SecureStateStore


@dataclass(frozen=True)
class BoardView:
    board: str
    titles: Mapping[str, str]
    assignees: Mapping[str, str | None]
    triage_task: str | None


@dataclass(frozen=True)
class GateSnapshot:
    fingerprint: str
    sample: HostSample
    config: ControllerConfig
    estop: bytes | None
    manual_hold: bool
    workers: Sequence[LiveWorker]
    boards: tuple[BoardView, ...]
    unowned_subscriptions: int


@dataclass(frozen=True)
class CommandOutcome:
    uncertain: bool
    actual_task_id: str | None


@dataclass(frozen=True)
class TickResult:
    reason: str
    priority_misses: int


class World(Protocol):
    def capture(self) -> GateSnapshot: ...


class Commands(Protocol):
    def predict(self, board: BoardView, failure_limit: int) -> PredictedPick: ...
    def dispatch(self, pick: PredictedPick, failure_limit: int) -> CommandOutcome: ...
    def decompose(self, board: str, task_id: str) -> CommandOutcome: ...


class ControllerEngine:
    """Admission-only state machine; all host and command I/O is injected."""

    def __init__(
        self,
        policy: AdmissionPolicy,
        store: SecureStateStore,
        world: World,
        commands: Commands,
    ) -> None:
        self.policy = policy
        self.store = store
        self.world = world
        self.commands = commands
        self.pointer = 0
        self.passed: dict[str, int] = {}
        self.priority_misses = 0

    def tick(self) -> TickResult:
        if self.store.has_pending_uncertainty():
            return self._result("uncertain-outcome")
        initial = self.world.capture()
        health = self.policy.observe(initial.sample)
        if not health.eligible:
            return self._result(health.reason)
        hold = self._common_hold(initial)
        if hold:
            return self._result(hold)

        decomposition = self._choose_decomposition(initial)
        if decomposition is not None:
            board, task_id = decomposition
            return self._execute(
                initial,
                kind="decompose",
                board=board,
                task_id=task_id,
                profile=None,
                predicted=None,
            )

        predictions: list[PredictedPick] = []
        for board in initial.boards:
            try:
                predictions.append(self.commands.predict(board, initial.config.failure_limit))
            except Exception:
                # Binding amendment: prediction ambiguity makes this board only ineligible.
                continue
        if not predictions:
            self.pointer = (self.pointer + 1) % len(initial.boards)
            return self._result("prediction-unavailable")
        order = [board.board for board in initial.boards]
        selection = select_pick(
            predictions,
            board_order=order,
            pointer=self.pointer,
            passed=self.passed,
        )
        self.pointer = selection.next_pointer
        pick = selection.pick
        capacity = admission_capacity(initial.workers, board=pick.board, profile=pick.assignee or "")
        if not capacity.available:
            return self._result(capacity.reason)
        return self._execute(
            initial,
            kind="dispatch",
            board=pick.board,
            task_id=pick.task_id,
            profile=pick.assignee,
            predicted=pick,
            selection_passed=selection.passed,
        )

    def _common_hold(self, snapshot: GateSnapshot) -> str | None:
        if snapshot.estop is not None:
            return "estop"
        if snapshot.manual_hold:
            return "manual-hold"
        if snapshot.unowned_subscriptions:
            return "unowned-subscriptions"
        if len(snapshot.workers) >= 2:
            return "host-cap"
        if not snapshot.boards:
            return "no-boards"
        return None

    def _choose_decomposition(self, snapshot: GateSnapshot) -> tuple[str, str] | None:
        if not snapshot.config.auto_decompose:
            return None
        order = [board.board for board in snapshot.boards]
        rotated = order[self.pointer :] + order[: self.pointer]
        by_name = {board.board: board for board in snapshot.boards}
        for board_name in rotated:
            task_id = by_name[board_name].triage_task
            if task_id:
                self.pointer = (order.index(board_name) + 1) % len(order)
                return board_name, task_id
        return None

    def _execute(
        self,
        initial: GateSnapshot,
        *,
        kind: str,
        board: str,
        task_id: str,
        profile: str | None,
        predicted: PredictedPick | None,
        selection_passed: dict[str, int] | None = None,
    ) -> TickResult:
        final = self.world.capture()
        final_health = self.policy.observe(final.sample)
        if not final_health.eligible:
            return self._result(final_health.reason)
        if final.fingerprint != initial.fingerprint:
            return self._result("precommand-race")
        hold = self._common_hold(final)
        if hold:
            return self._result(hold)
        if kind == "dispatch":
            assert profile is not None and predicted is not None
            capacity = admission_capacity(final.workers, board=board, profile=profile)
            if not capacity.available:
                return self._result(capacity.reason)

        if selection_passed is not None:
            self.passed = selection_passed
        journal: dict[str, object] = {
            "outcome": "pending",
            "kind": kind,
            "board": board,
            "task_id": task_id,
            "fingerprint": final.fingerprint,
        }
        self.store.write_json("pending.json", journal)
        self.policy.command_consumed(final.sample.monotonic)
        try:
            if kind == "decompose":
                outcome = self.commands.decompose(board, task_id)
            else:
                outcome = self.commands.dispatch(predicted, final.config.failure_limit)  # type: ignore[arg-type]
        except BaseException:
            return self._result("uncertain-outcome")
        if outcome.uncertain:
            return self._result("uncertain-outcome")

        journal["outcome"] = "reconciled"
        journal["actual_task_id"] = outcome.actual_task_id
        self.store.write_json("pending.json", journal)
        if kind == "dispatch" and outcome.actual_task_id not in (None, task_id):
            self.priority_misses += 1
            return self._result("priority-miss")
        if kind == "decompose":
            return self._result("decomposed")
        return self._result("dispatched" if outcome.actual_task_id else "dispatch-noop")

    def _result(self, reason: str) -> TickResult:
        self.store.write_json(
            "status.json",
            {
                "mode": "best-effort downstream-first",
                "reason": reason,
                "priority_misses": self.priority_misses,
                "passed_windows": self.passed,
                "round_robin_pointer": self.pointer,
            },
        )
        return TickResult(reason, self.priority_misses)
