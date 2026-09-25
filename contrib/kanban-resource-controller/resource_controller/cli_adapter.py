from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from .cli_contract import (
    CommandContractError,
    build_decompose_command,
    build_dispatch_command,
    build_dry_run_command,
    parse_dispatch_prediction,
    validate_dispatch_payload,
)
from .engine import BoardView, CommandOutcome, ErrorDetail
from .priority import PredictedPick
from .supervision import CommandResult, run_supervised

Runner = Callable[[list[str], float, int], CommandResult]
ReconcileActual = Callable[[str, str], Optional[str]]

_DECOMPOSE_KEYS = {"task_id", "ok", "reason", "fanout", "child_ids", "new_title"}


def _default_runner(argv: list[str], timeout: float, limit: int) -> CommandResult:
    return run_supervised(argv, observe_timeout=timeout, max_output=limit)


class CliCommands:
    """Supported Hermes CLI adapter; no private dispatcher imports."""

    def __init__(
        self,
        executable: Path,
        *,
        runner: Runner = _default_runner,
        reconcile_actual: ReconcileActual,
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("Hermes executable must be absolute")
        self.executable = executable
        self.runner = runner
        self.reconcile_actual = reconcile_actual

    def predict(self, board: BoardView, failure_limit: int, dispatch_max: int) -> PredictedPick:
        result = self.runner(
            build_dry_run_command(self.executable, board.board, failure_limit, dispatch_max),
            15.0,
            65536,
        )
        if result.uncertain:
            raise CommandContractError("dry-run outcome uncertain")
        return parse_dispatch_prediction(
            result.stdout,
            board=board.board,
            titles=board.titles,
            assignees=board.assignees,
            eligible_profiles=board.eligible_profiles or None,
        )

    def dispatch(self, pick: PredictedPick, failure_limit: int, dispatch_max: int) -> CommandOutcome:
        result = self.runner(
            build_dispatch_command(self.executable, pick.board, failure_limit, dispatch_max),
            30.0,
            65536,
        )
        if result.uncertain:
            return CommandOutcome(True, error=_result_error(result))
        try:
            claims = _dispatch_claims(result.stdout, dispatch_max)
        except (CommandContractError, TypeError, ValueError) as exc:
            return CommandOutcome(True, error=ErrorDetail("ContractDrift", str(exc)))
        return CommandOutcome(False, tuple(task_id for task_id, _ in claims), claims)

    def decompose(self, board: str, task_id: str) -> CommandOutcome:
        result = self.runner(
            build_decompose_command(self.executable, board, task_id),
            120.0,  # decompose subprocess wall-clock timeout (not admission pacing)
            65536,
        )
        if result.uncertain:
            return CommandOutcome(True, error=_result_error(result))
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return CommandOutcome(True, error=ErrorDetail("ContractDrift", str(exc)))
        if (
            not isinstance(payload, dict)
            or set(payload) != _DECOMPOSE_KEYS
            or payload.get("task_id") != task_id
            or payload.get("ok") is not True
        ):
            return CommandOutcome(True, error=ErrorDetail("ContractDrift", "decompose payload contract mismatch"))
        try:
            unexpected_worker = self.reconcile_actual(board, task_id)
        except Exception as exc:
            return CommandOutcome(True, error=ErrorDetail("AmbiguousReconciliation", str(exc)))
        if unexpected_worker is not None:
            return CommandOutcome(
                True,
                error=ErrorDetail("AmbiguousReconciliation", "decompose started an unexpected worker"),
            )
        return CommandOutcome(False, (task_id,))


def _dispatch_claims(output: str, dispatch_max: int) -> tuple[tuple[str, str], ...]:
    payload = validate_dispatch_payload(output)
    if payload["skipped_locked"]:
        # A second dispatcher owned the tick: dual authority, never a clean miss.
        raise CommandContractError("another dispatcher holds the board tick lock")
    rows = payload["spawned"]
    if len(rows) > dispatch_max:
        raise CommandContractError("dispatch reported more starts than its maximum")
    claims: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"task_id", "assignee", "workspace"}:
            raise CommandContractError("spawned row shape mismatch")
        task_id = row["task_id"]
        assignee = row["assignee"]
        if not isinstance(task_id, str) or not task_id or not isinstance(assignee, str) or not assignee:
            raise CommandContractError("spawned row identity is invalid")
        claims.append((task_id, assignee))
    if len({task_id for task_id, _ in claims}) != len(claims):
        raise CommandContractError("duplicate spawned task identity")
    return tuple(claims)


def _result_error(result: CommandResult) -> ErrorDetail:
    if result.timed_out:
        return ErrorDetail("CommandTimeout", "command exceeded its observation deadline")
    if result.truncated:
        return ErrorDetail("CommandTruncated", "command output exceeded its bounded capture")
    return ErrorDetail("CommandExit", f"command exited with status {result.returncode}")
