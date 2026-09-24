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
from .engine import BoardView, CommandOutcome
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

    def predict(self, board: BoardView, failure_limit: int) -> PredictedPick:
        result = self.runner(
            build_dry_run_command(self.executable, board.board, failure_limit),
            15.0,
            65536,
        )
        if result.uncertain:
            raise CommandContractError("dry-run outcome uncertain")
        return parse_dispatch_prediction(result.stdout, board=board.board, titles=board.titles)

    def dispatch(self, pick: PredictedPick, failure_limit: int) -> CommandOutcome:
        result = self.runner(
            build_dispatch_command(self.executable, pick.board, failure_limit),
            30.0,
            65536,
        )
        if result.uncertain or not _valid_dispatch_json(result.stdout):
            return CommandOutcome(True, None)
        try:
            actual = self.reconcile_actual(pick.board, pick.task_id)
        except Exception:
            return CommandOutcome(True, None)
        return CommandOutcome(False, actual)

    def decompose(self, board: str, task_id: str) -> CommandOutcome:
        result = self.runner(
            build_decompose_command(self.executable, board, task_id),
            120.0,
            65536,
        )
        if result.uncertain:
            return CommandOutcome(True, None)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return CommandOutcome(True, None)
        if (
            not isinstance(payload, dict)
            or set(payload) != _DECOMPOSE_KEYS
            or payload.get("task_id") != task_id
            or payload.get("ok") is not True
        ):
            return CommandOutcome(True, None)
        try:
            unexpected_worker = self.reconcile_actual(board, task_id)
        except Exception:
            return CommandOutcome(True, None)
        if unexpected_worker is not None:
            return CommandOutcome(True, None)
        return CommandOutcome(False, task_id)


def _valid_dispatch_json(output: str) -> bool:
    try:
        payload = validate_dispatch_payload(output)
    except CommandContractError:
        return False
    if payload["skipped_locked"]:
        # A second dispatcher owned the tick: dual authority, never a clean miss.
        return False
    return len(payload["spawned"]) <= 1
