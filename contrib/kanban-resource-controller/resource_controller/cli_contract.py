from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .priority import PredictedPick


class CommandContractError(ValueError):
    pass


def _prefix(executable: Path, board: str) -> list[str]:
    if not executable.is_absolute():
        raise CommandContractError("Hermes executable must be absolute")
    if not board or board.startswith("-"):
        raise CommandContractError("invalid board slug")
    return [str(executable), "kanban", "--board", board]


def build_dry_run_command(executable: Path, board: str, failure_limit: int) -> list[str]:
    return _prefix(executable, board) + [
        "dispatch", "--dry-run", "--max", "1", "--failure-limit", str(failure_limit), "--json"
    ]


def build_dispatch_command(executable: Path, board: str, failure_limit: int) -> list[str]:
    return _prefix(executable, board) + [
        "dispatch", "--max", "1", "--failure-limit", str(failure_limit), "--json"
    ]


def build_decompose_command(executable: Path, board: str, task_id: str) -> list[str]:
    if not task_id or task_id.startswith("-"):
        raise CommandContractError("invalid task id")
    return _prefix(executable, board) + [
        "decompose", task_id, "--author", "auto-decomposer", "--json"
    ]


_DISPATCH_KEYS = {
    "reclaimed",
    "crashed",
    "timed_out",
    "stale",
    "auto_blocked",
    "promoted",
    "spawned",
    "skipped_unassigned",
    "skipped_nonspawnable",
    "skipped_per_profile_capped",
    "auto_assigned_default",
}


def parse_dispatch_prediction(
    output: str,
    *,
    board: str,
    titles: Mapping[str, str],
) -> PredictedPick:
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CommandContractError("dispatch output is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _DISPATCH_KEYS:
        raise CommandContractError("dispatch output contract mismatch")
    if (
        payload["reclaimed"] != 0
        or payload["promoted"] != 0
        or any(payload[key] for key in ("crashed", "timed_out", "stale", "auto_blocked"))
    ):
        raise CommandContractError("dry-run predicted maintenance changes")
    spawned = payload["spawned"]
    if not isinstance(spawned, list) or len(spawned) != 1 or not isinstance(spawned[0], dict):
        raise CommandContractError("dry-run must predict exactly one task")
    row = spawned[0]
    if set(row) != {"task_id", "assignee", "workspace"}:
        raise CommandContractError("spawn prediction shape mismatch")
    task_id = row["task_id"]
    assignee = row["assignee"]
    if not isinstance(task_id, str) or not isinstance(assignee, str):
        raise CommandContractError("spawn prediction identity is invalid")
    title = titles.get(task_id)
    if not isinstance(title, str) or not title:
        raise CommandContractError("predicted task missing from canonical inventory")
    return PredictedPick(board, task_id, assignee, title)
