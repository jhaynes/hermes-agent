from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from .spec import AdmissionCaps
from .worker_identity import IdentityHold, StoredFingerprint, matches


_TASK_PATTERN = re.compile(r"^t_[A-Za-z0-9]+$")
_VALUE_OPTIONS = {"-p", "--skills", "-m", "--provider", "--reasoning", "--toolsets"}
_FLAG_OPTIONS = {"--cli", "--accept-hooks"}


@dataclass(frozen=True)
class WorkerCommand:
    profile: str
    task_id: str


def parse_worker_argv(argv: Sequence[str]) -> WorkerCommand:
    try:
        profile_index = argv.index("-p")
        chat_index = argv.index("chat", profile_index + 2)
    except ValueError as exc:
        raise IdentityHold("not canonical worker argv") from exc
    if profile_index < 1 or profile_index + 1 >= len(argv):
        raise IdentityHold("missing worker profile")
    profile = argv[profile_index + 1]
    if not profile or profile.startswith("-"):
        raise IdentityHold("invalid worker profile")

    seen_flags: set[str] = set()
    index = profile_index + 2
    while index < chat_index:
        item = argv[index]
        if item in _FLAG_OPTIONS:
            seen_flags.add(item)
            index += 1
        elif item in _VALUE_OPTIONS:
            if index + 1 >= chat_index or not argv[index + 1]:
                raise IdentityHold(f"missing value for {item}")
            index += 2
        else:
            raise IdentityHold(f"unknown worker option {item}")
    if seen_flags != _FLAG_OPTIONS:
        raise IdentityHold("canonical worker flags missing")

    suffix = list(argv[chat_index + 1 :])
    if len(suffix) not in (2, 3) or suffix[0] != "-q":
        raise IdentityHold("invalid worker chat suffix")
    if len(suffix) == 3 and suffix[2] != "-Q":
        raise IdentityHold("invalid worker quiet flag")
    prefix = "work kanban task "
    if not suffix[1].startswith(prefix):
        raise IdentityHold("invalid worker prompt")
    task_id = suffix[1][len(prefix) :]
    if not _TASK_PATTERN.fullmatch(task_id):
        raise IdentityHold("invalid worker task id")
    return WorkerCommand(profile, task_id)


@dataclass(frozen=True)
class CanonicalRun:
    board: str
    task_id: str
    run_id: int
    pid: int
    worker_fingerprint: StoredFingerprint
    profile: str
    task_status: str


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    created_at: float
    ppid: int
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    accessible: bool
    start_fingerprint: int | None = None


@dataclass(frozen=True)
class LiveWorker:
    board: str
    task_id: str
    run_id: int
    pid: int
    created_at: float
    profile: str
    task_status: str
    worker_fingerprint: str = ""


@dataclass(frozen=True)
class Capacity:
    available: bool
    reason: str
    name: str | None = None
    count: int | None = None
    cap: int | None = None


def reconcile_workers(
    runs: Sequence[CanonicalRun],
    processes: Sequence[ProcessSnapshot],
    *,
    epoch: str = "",
) -> list[LiveWorker]:
    by_pid: dict[int, CanonicalRun] = {}
    identities: set[tuple[int, str]] = set()
    for run in runs:
        if run.pid in by_pid:
            raise IdentityHold(f"duplicate canonical pid {run.pid}")
        identity = (run.pid, run.worker_fingerprint.raw)
        if identity in identities:
            raise IdentityHold("duplicate process identity")
        identities.add(identity)
        by_pid[run.pid] = run

    matched: set[int] = set()
    workers: list[LiveWorker] = []
    for process in processes:
        marked = bool(process.environment.get("HERMES_KANBAN_TASK"))
        try:
            command = parse_worker_argv(process.argv)
        except IdentityHold:
            if marked:
                raise IdentityHold(f"unrecognized marked process {process.pid}")
            continue
        if not process.accessible:
            raise IdentityHold(f"worker process {process.pid} is inaccessible")
        run = by_pid.get(process.pid)
        if run is None:
            raise IdentityHold(f"worker process {process.pid} has no canonical run")
        if process.pid in matched:
            raise IdentityHold(f"duplicate process snapshot {process.pid}")
        if process.start_fingerprint is None:
            raise IdentityHold(f"identity-unavailable: worker process {process.pid} start time unreadable")
        current = f"{epoch}|{process.start_fingerprint}"
        if not matches(run.worker_fingerprint, current):
            raise IdentityHold(f"identity-mismatch: PID reuse for {process.pid}")
        expected = {
            "HERMES_KANBAN_TASK": run.task_id,
            "HERMES_KANBAN_RUN_ID": str(run.run_id),
            "HERMES_KANBAN_BOARD": run.board,
            "HERMES_PROFILE": run.profile,
        }
        if command.task_id != run.task_id or command.profile != run.profile:
            raise IdentityHold(f"argv mismatch for {run.task_id}")
        if any(process.environment.get(key) != value for key, value in expected.items()):
            raise IdentityHold(f"environment marker mismatch for {run.task_id}")
        matched.add(process.pid)
        workers.append(
            LiveWorker(
                run.board,
                run.task_id,
                run.run_id,
                run.pid,
                process.created_at,
                run.profile,
                run.task_status,
                run.worker_fingerprint.raw,
            )
        )
    missing = set(by_pid) - matched
    if missing:
        raise IdentityHold(f"canonical worker process missing: {min(missing)}")
    return workers


def admission_capacity(
    workers: Sequence[LiveWorker],
    *,
    caps: AdmissionCaps,
    board: str,
    profile: str,
) -> Capacity:
    controller_reconciled_live_count = len(workers)
    if controller_reconciled_live_count >= caps.host_cap:
        return Capacity(False, "host-cap", "host", controller_reconciled_live_count, caps.host_cap)
    profile_count = sum(worker.profile == profile for worker in workers)
    profile_cap = caps.for_profile(profile)
    if profile_count >= profile_cap:
        return Capacity(False, "profile-cap", profile, profile_count, profile_cap)
    board_count = sum(worker.board == board for worker in workers)
    board_cap = caps.for_board(board)
    if board_count >= board_cap:
        return Capacity(False, "board-cap", board, board_count, board_cap)
    return Capacity(True, "available")


def existing_capacity_violation(
    workers: Sequence[LiveWorker],
    caps: AdmissionCaps,
) -> Capacity | None:
    controller_reconciled_live_count = len(workers)
    if controller_reconciled_live_count > caps.host_cap:
        return Capacity(False, "host-cap", "host", controller_reconciled_live_count, caps.host_cap)
    for profile in sorted({worker.profile for worker in workers}):
        count = sum(worker.profile == profile for worker in workers)
        cap = caps.for_profile(profile)
        if count > cap:
            return Capacity(False, "profile-cap", profile, count, cap)
    for board in sorted({worker.board for worker in workers}):
        count = sum(worker.board == board for worker in workers)
        cap = caps.for_board(board)
        if count > cap:
            return Capacity(False, "board-cap", board, count, cap)
    return None


def dispatch_maximum(
    caps: AdmissionCaps,
    *,
    board: str,
    hermes_db_running_count: int,
) -> int | None:
    if (
        not isinstance(hermes_db_running_count, int)
        or isinstance(hermes_db_running_count, bool)
        or hermes_db_running_count < 0
    ):
        raise ValueError("hermes_db_running_count must be a nonnegative integer")
    board_cap = caps.for_board(board)
    if hermes_db_running_count >= board_cap:
        return None
    return min(board_cap, hermes_db_running_count + 1)
