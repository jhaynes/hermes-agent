from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Mapping, Protocol, Sequence

from .cli_contract import PrecommandRace
from .config import ControllerConfig
from .inventory import (
    Capacity,
    LiveWorker,
    admission_capacity,
    dispatch_maximum,
    existing_capacity_violation,
)
from .policy import AdmissionPolicy, HostSample
from .priority import PredictedPick, select_pick
from .storage import SecureStateStore
from .telemetry import sanitize_diagnostic
from .telemetry import constants as telemetry_constants


# Samples with these reasons start a fresh baseline; a delta against the prior
# sample would span an unknown gap, a reset, or an untrusted sample.
_NO_DELTA_REASONS = frozenset({
    telemetry_constants.REASON_SWAP_BASELINE,
    telemetry_constants.REASON_SAMPLE_GAP,
    telemetry_constants.REASON_SWAP_RESET,
    telemetry_constants.REASON_NONMONOTONIC_TIME,
    telemetry_constants.REASON_UNKNOWN_TELEMETRY,
})


@dataclass(frozen=True)
class BoardView:
    board: str
    titles: Mapping[str, str]
    assignees: Mapping[str, str | None]
    triage_task: str | None
    hermes_db_running_count: int = 0
    eligible_profiles: tuple[str, ...] = ()


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
class ErrorDetail:
    type: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", sanitize_diagnostic(self.type))
        object.__setattr__(self, "message", sanitize_diagnostic(self.message))

    @classmethod
    def from_exception(cls, error: BaseException) -> "ErrorDetail":
        return cls(type(error).__name__, str(error))


@dataclass(frozen=True)
class CommandOutcome:
    uncertain: bool
    actual_task_ids: tuple[str, ...] = ()
    spawned: tuple[tuple[str, str], ...] = ()
    error: ErrorDetail | None = None

    @property
    def actual_task_id(self) -> str | None:
        return self.actual_task_ids[0] if self.actual_task_ids else None


@dataclass(frozen=True)
class TickResult:
    reason: str
    priority_misses: int


class World(Protocol):
    def capture(self) -> GateSnapshot: ...


class Commands(Protocol):
    def predict(self, board: BoardView, failure_limit: int, dispatch_max: int) -> PredictedPick: ...
    def dispatch(self, pick: PredictedPick, failure_limit: int, dispatch_max: int) -> CommandOutcome: ...
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
        self._previous_sample: HostSample | None = None
        self._previous_wall_time: float | None = None

    def invalidate(self) -> None:
        """Telemetry failed: forget the policy baseline/dwell and the status delta baseline."""
        self.policy.invalidate()
        self._previous_sample = None
        self._previous_wall_time = None

    def tick(self) -> TickResult:
        if self.store.has_pending_uncertainty():
            return self._result(
                "uncertain-outcome",
                sample=None,
                error=_pending_error(self.store.read_json("pending.json")),
            )
        initial = self.world.capture()
        health = self.policy.observe(initial.sample)
        if not health.eligible:
            return self._result(health.reason, sample=initial.sample, observed_blockers=health.observed_blockers)
        hold = self._common_hold(initial)
        if hold:
            return self._result(hold, sample=initial.sample, observed_blockers=health.observed_blockers)

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
        board_caps_held = 0
        for board in initial.boards:
            if (
                sum(worker.board == board.board for worker in initial.workers)
                >= initial.config.admission_caps.for_board(board.board)
            ):
                board_caps_held += 1
                continue
            dispatch_max = dispatch_maximum(
                initial.config.admission_caps,
                board=board.board,
                hermes_db_running_count=board.hermes_db_running_count,
            )
            if dispatch_max is None:
                continue
            try:
                pick = self.commands.predict(board, initial.config.failure_limit, dispatch_max)
                if pick.assignee not in initial.config.dispatch_profiles:
                    continue
                predictions.append(pick)
            except PrecommandRace:
                return self._result(
                    "precommand-race", sample=initial.sample,
                    observed_blockers=health.observed_blockers,
                )
            except Exception:
                # Binding amendment: prediction ambiguity makes this board only ineligible.
                continue
        if not predictions:
            if board_caps_held == len(initial.boards):
                return self._result(
                    "board-cap", sample=initial.sample,
                    observed_blockers=health.observed_blockers,
                )
            self.pointer = (self.pointer + 1) % len(initial.boards)
            return self._result("prediction-unavailable", sample=initial.sample, observed_blockers=health.observed_blockers)
        order = [board.board for board in initial.boards]
        selection = select_pick(
            predictions,
            board_order=order,
            pointer=self.pointer,
            passed=self.passed,
        )
        self.pointer = selection.next_pointer
        pick = selection.pick
        capacity = admission_capacity(
            initial.workers,
            caps=initial.config.admission_caps,
            board=pick.board,
            profile=pick.assignee or "",
        )
        if not capacity.available:
            return self._result(capacity.reason, sample=initial.sample, observed_blockers=health.observed_blockers)
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
        if len(snapshot.workers) >= snapshot.config.admission_caps.host_cap:
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
            return self._result(final_health.reason, sample=final.sample, observed_blockers=final_health.observed_blockers)
        if final.fingerprint != initial.fingerprint:
            return self._result("precommand-race", sample=final.sample, observed_blockers=final_health.observed_blockers)
        hold = self._common_hold(final)
        if hold:
            return self._result(hold, sample=final.sample, observed_blockers=final_health.observed_blockers)
        if kind == "dispatch":
            assert profile is not None and predicted is not None
            capacity = admission_capacity(
                final.workers,
                caps=final.config.admission_caps,
                board=board,
                profile=profile,
            )
            if not capacity.available:
                return self._result(capacity.reason, sample=final.sample, observed_blockers=final_health.observed_blockers)
            board_view = next(item for item in final.boards if item.board == board)
            dispatch_max = dispatch_maximum(
                final.config.admission_caps,
                board=board,
                hermes_db_running_count=board_view.hermes_db_running_count,
            )
            if dispatch_max is None:
                return self._result("board-cap", sample=final.sample, observed_blockers=final_health.observed_blockers)

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
                outcome = self.commands.dispatch(  # type: ignore[arg-type]
                    predicted, final.config.failure_limit, dispatch_max,
                )
        except BaseException as exc:
            return self._uncertain(
                journal, ErrorDetail.from_exception(exc), final.sample,
                observed_blockers=final_health.observed_blockers,
            )
        if outcome.uncertain:
            return self._uncertain(
                journal,
                outcome.error or ErrorDetail("AmbiguousCommand", "command outcome is uncertain"),
                final.sample,
                observed_blockers=final_health.observed_blockers,
            )

        violation: Capacity | None = None
        if kind == "dispatch":
            try:
                post = self.world.capture()
                actual_task_ids, violation = self._reconcile_dispatch(
                    final, post, outcome, predicted, dispatch_max,  # type: ignore[arg-type]
                )
            except Exception as exc:
                return self._uncertain(
                    journal, ErrorDetail.from_exception(exc), final.sample,
                    observed_blockers=final_health.observed_blockers,
                )
            if violation is not None:
                return self._uncertain(
                    journal,
                    ErrorDetail(
                        "CapacityViolation",
                        f"{violation.reason} {violation.name}: {violation.count} exceeds {violation.cap}",
                    ),
                    post.sample,
                    observed_blockers=final_health.observed_blockers,
                    cap_violation=violation,
                )
        else:
            actual_task_ids = outcome.actual_task_ids

        journal["outcome"] = "reconciled"
        journal["actual_task_ids"] = list(actual_task_ids)
        journal["actual_task_id"] = actual_task_ids[0] if actual_task_ids else None
        journal["extra_starts"] = max(0, len(actual_task_ids) - 1)
        self.store.write_json("pending.json", journal)
        if kind == "dispatch" and actual_task_ids and actual_task_ids[0] != task_id:
            self.priority_misses += 1
            return self._result(
                "priority-miss", sample=final.sample,
                observed_blockers=final_health.observed_blockers,
                actual_task_ids=actual_task_ids,
            )
        if kind == "decompose":
            return self._result("decomposed", sample=final.sample, observed_blockers=final_health.observed_blockers)
        return self._result(
            "dispatched" if actual_task_ids else "dispatch-noop",
            sample=final.sample,
            observed_blockers=final_health.observed_blockers,
            actual_task_ids=actual_task_ids,
        )

    def _uncertain(
        self,
        journal: dict[str, object],
        error: ErrorDetail,
        sample: HostSample,
        *,
        observed_blockers: tuple[str, ...] = (),
        cap_violation: Capacity | None = None,
    ) -> TickResult:
        journal["outcome"] = "pending"
        journal["error"] = {"type": error.type, "message": error.message}
        self.store.write_json("pending.json", journal)
        return self._result(
            "uncertain-outcome",
            sample=sample,
            observed_blockers=observed_blockers,
            cap_violation=cap_violation,
            error=error,
        )

    def _reconcile_dispatch(
        self,
        baseline: GateSnapshot,
        post: GateSnapshot,
        outcome: CommandOutcome,
        predicted: PredictedPick,
        dispatch_max: int,
    ) -> tuple[tuple[str, ...], Capacity | None]:
        claims = outcome.spawned
        if len(claims) > dispatch_max or len({task_id for task_id, _ in claims}) != len(claims):
            raise RuntimeError("invalid dispatch claim set")
        board = next(item for item in baseline.boards if item.board == predicted.board)
        for task_id, profile in claims:
            if task_id not in board.titles or board.assignees.get(task_id) != profile:
                raise RuntimeError("dispatch claim is outside the fenced candidate set")
            if profile not in baseline.config.dispatch_profiles:
                raise RuntimeError("dispatch claim uses an ineligible profile")
        before = {_worker_identity(worker) for worker in baseline.workers}
        after = {_worker_identity(worker) for worker in post.workers}
        new_workers = after - before
        claimed = {(task_id, profile) for task_id, profile in claims}
        observed = {(identity[0], identity[5]) for identity in new_workers}
        if observed != claimed or len(new_workers) != len(claims):
            raise RuntimeError("CLI claims do not match newly reconciled worker identities")
        if post.config != baseline.config:
            raise RuntimeError("configuration changed after dispatch")
        return tuple(task_id for task_id, _ in claims), existing_capacity_violation(
            post.workers, post.config.admission_caps,
        )

    def _result(
        self,
        reason: str,
        *,
        sample: HostSample | None = None,
        observed_blockers: tuple[str, ...] = (),
        actual_task_ids: tuple[str, ...] = (),
        cap_violation: Capacity | None = None,
        error: ErrorDetail | None = None,
    ) -> TickResult:
        payload: dict[str, object] = {
            "schema_version": telemetry_constants.SCHEMA_VERSION,
            "mode": "best-effort downstream-first",
            "reason": reason,
            "priority_misses": self.priority_misses,
            "passed_windows": self.passed,
            "round_robin_pointer": self.pointer,
            "observed_blockers": list(observed_blockers),
            "actual_task_ids": list(actual_task_ids),
            "actual_task_id": actual_task_ids[0] if actual_task_ids else None,
            "extra_starts": max(0, len(actual_task_ids) - 1),
        }
        if cap_violation is not None:
            payload["cap_violation"] = {
                "reason": cap_violation.reason,
                "name": cap_violation.name,
                "count": cap_violation.count,
                "cap": cap_violation.cap,
            }
        if error is not None:
            payload["error_type"] = error.type
            payload["error"] = error.message
        if sample is not None:
            wall_time = time.time()
            interval_seconds = None
            swap_in_delta = None
            swap_out_delta = None
            if self._previous_sample is not None and reason not in _NO_DELTA_REASONS:
                interval_seconds = sample.monotonic - self._previous_sample.monotonic
                if sample.swap_in >= self._previous_sample.swap_in:
                    swap_in_delta = sample.swap_in - self._previous_sample.swap_in
                if sample.swap_out >= self._previous_sample.swap_out:
                    swap_out_delta = sample.swap_out - self._previous_sample.swap_out
            payload.update({
                "pressure": sample.pressure,
                "pressure_detail": dict(sample.pressure_detail or {}),
                "swap_in_bytes": sample.swap_in,
                "swap_out_bytes": sample.swap_out,
                "counter_page_size_bytes": sample.counter_page_size_bytes,
                "units": "bytes",
                "source": sample.source,
                "swap_in_delta_bytes": swap_in_delta,
                "swap_out_delta_bytes": swap_out_delta,
                "sample_wall_time": wall_time,
                "interval_seconds": interval_seconds,
                "telemetry_age_seconds": 0.0,
            })
            self._previous_sample = sample
            self._previous_wall_time = wall_time
        self.store.write_json("status.json", payload)
        return TickResult(reason, self.priority_misses)


def _worker_identity(worker: LiveWorker) -> tuple[object, ...]:
    return (
        worker.task_id,
        worker.run_id,
        worker.pid,
        worker.created_at,
        worker.board,
        worker.profile,
        worker.worker_fingerprint,
    )


def _pending_error(payload: object) -> ErrorDetail:
    if isinstance(payload, dict):
        raw = payload.get("error")
        if (
            isinstance(raw, dict)
            and isinstance(raw.get("type"), str)
            and isinstance(raw.get("message"), str)
        ):
            return ErrorDetail(raw["type"], raw["message"])
    return ErrorDetail(
        "PendingUncertainty",
        "pending command outcome requires operator reconciliation",
    )
