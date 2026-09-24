from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .priority import PredictedPick


class CommandContractError(ValueError):
    pass


class PrecommandRace(CommandContractError):
    pass


def _prefix(executable: Path, board: str) -> list[str]:
    if not executable.is_absolute():
        raise CommandContractError("Hermes executable must be absolute")
    if not board or board.startswith("-"):
        raise CommandContractError("invalid board slug")
    return [str(executable), "kanban", "--board", board]


def build_dry_run_command(
    executable: Path,
    board: str,
    failure_limit: int,
    dispatch_max: int,
) -> list[str]:
    _positive_dispatch_max(dispatch_max)
    return _prefix(executable, board) + [
        "dispatch", "--dry-run", "--max", str(dispatch_max),
        "--failure-limit", str(failure_limit), "--json"
    ]


def build_dispatch_command(
    executable: Path,
    board: str,
    failure_limit: int,
    dispatch_max: int,
) -> list[str]:
    _positive_dispatch_max(dispatch_max)
    return _prefix(executable, board) + [
        "dispatch", "--max", str(dispatch_max), "--failure-limit", str(failure_limit), "--json"
    ]


def _positive_dispatch_max(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CommandContractError("dispatch maximum must be a positive integer")


def build_decompose_command(executable: Path, board: str, task_id: str) -> list[str]:
    if not task_id or task_id.startswith("-"):
        raise CommandContractError("invalid task id")
    return _prefix(executable, board) + [
        "decompose", task_id, "--author", "auto-decomposer", "--json"
    ]


# Exact key set of `hermes kanban dispatch --json` at the pinned source
# (hermes_cli/kanban_ops.py::_cmd_dispatch). Any addition or removal is drift.
DISPATCH_KEYS = frozenset({
    "reclaimed",
    "crashed",
    "timed_out",
    "stale",
    "auto_blocked",
    "promoted",
    "reaped_terminal_workers",
    "spawned",
    "skipped_unassigned",
    "skipped_nonspawnable",
    "skipped_per_profile_capped",
    "auto_assigned_default",
    "respawn_guarded",
    "rate_limited",
    "skipped_locked",
    "memory_pressure",
})
_LIST_KEYS = DISPATCH_KEYS - {"reclaimed", "promoted", "skipped_locked", "memory_pressure"}
# Tick side effects the CLI performs even under --dry-run; a prediction taken
# while any are pending is not a clean read.
_MAINTENANCE_LISTS = ("crashed", "timed_out", "stale", "auto_blocked", "reaped_terminal_workers", "rate_limited")


def validate_dispatch_payload(output: str) -> dict:
    """Parse and type-check dispatch JSON; raise on any shape drift."""
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CommandContractError("dispatch output is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != DISPATCH_KEYS:
        raise CommandContractError("dispatch output contract mismatch")
    for key in ("reclaimed", "promoted"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise CommandContractError(f"dispatch field {key} has wrong type")
    for key in _LIST_KEYS:
        if not isinstance(payload[key], list):
            raise CommandContractError(f"dispatch field {key} has wrong type")
    if not isinstance(payload["skipped_locked"], bool):
        raise CommandContractError("dispatch field skipped_locked has wrong type")
    if payload["memory_pressure"] is not None and not isinstance(payload["memory_pressure"], str):
        raise CommandContractError("dispatch field memory_pressure has wrong type")
    return payload


def parse_dispatch_prediction(
    output: str,
    *,
    board: str,
    titles: Mapping[str, str],
    assignees: Mapping[str, str | None] | None = None,
    eligible_profiles: tuple[str, ...] | None = None,
) -> PredictedPick:
    payload = validate_dispatch_payload(output)
    if payload["skipped_locked"]:
        raise CommandContractError("another dispatcher holds the board tick lock")
    if payload["memory_pressure"] is not None:
        raise CommandContractError("Hermes reported memory pressure")
    if (
        payload["reclaimed"] != 0
        or payload["promoted"] != 0
        or any(payload[key] for key in _MAINTENANCE_LISTS)
    ):
        raise CommandContractError("dry-run predicted maintenance changes")
    spawned = payload["spawned"]
    if not isinstance(spawned, list) or not spawned:
        raise CommandContractError("dry-run must predict one task")
    validated: list[tuple[str, str, str]] = []
    for row in spawned:
        if not isinstance(row, dict) or set(row) != {"task_id", "assignee", "workspace"}:
            raise CommandContractError("spawn prediction shape mismatch")
        task_id = row["task_id"]
        assignee = row["assignee"]
        if not isinstance(task_id, str) or not isinstance(assignee, str):
            raise CommandContractError("spawn prediction identity is invalid")
        title = titles.get(task_id)
        if not isinstance(title, str) or not title:
            raise CommandContractError("predicted task missing from selected board inventory")
        if assignees is not None and assignees.get(task_id) != assignee:
            raise CommandContractError("predicted assignee differs from fenced board inventory")
        if eligible_profiles is not None and assignee not in eligible_profiles:
            raise CommandContractError("predicted profile is not dispatch-eligible")
        validated.append((task_id, assignee, title))
    if len(validated) > 1:
        raise PrecommandRace("dry-run exposed more than one open slot")
    task_id, assignee, title = validated[0]
    return PredictedPick(board, task_id, assignee, title)
