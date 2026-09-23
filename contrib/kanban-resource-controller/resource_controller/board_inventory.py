from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
from typing import Mapping
from urllib.parse import quote

from .inventory import CanonicalRun


class BoardInventoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoardSnapshot:
    board: str
    titles: Mapping[str, str]
    assignees: Mapping[str, str | None]
    statuses: Mapping[str, str]
    runs: tuple[CanonicalRun, ...]
    unowned_subscriptions: int


_REQUIRED_COLUMNS = {
    "tasks": {
        "id", "title", "assignee", "status", "priority", "created_at",
        "worker_pid", "worker_started_at", "current_run_id"
    },
    "task_runs": {"id", "task_id", "profile", "status"},
    "kanban_notify_subs": {"task_id", "notifier_profile"},
}


def read_board_inventory(path: Path, *, board: str, max_rows: int = 10_000) -> BoardSnapshot:
    if max_rows <= 0:
        raise BoardInventoryError("row bound must be positive")
    if path.is_symlink():
        raise BoardInventoryError("board database must not be a symlink")
    try:
        info = path.stat()
    except OSError as exc:
        raise BoardInventoryError(f"cannot stat board database: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BoardInventoryError("board database must be a regular file")

    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        _validate_schema(connection)
        task_count = int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        if task_count > max_rows:
            raise BoardInventoryError(f"task row bound exceeded: {task_count} > {max_rows}")
        candidates = connection.execute(
            "SELECT id, title, assignee, status FROM tasks "
            "WHERE status IN ('triage','todo','ready','review') "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        run_rows = connection.execute(
            "SELECT t.id AS task_id, t.status AS task_status, t.worker_pid, "
            "t.worker_started_at, t.current_run_id, t.assignee, r.id AS run_id, "
            "r.task_id AS run_task_id, r.profile "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.worker_pid IS NOT NULL"
        ).fetchall()
        if len(run_rows) > max_rows:
            raise BoardInventoryError("active run row bound exceeded")
        runs = tuple(_canonical_run(row, board) for row in run_rows)
        unowned = int(
            connection.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs "
                "WHERE notifier_profile IS NULL OR TRIM(notifier_profile) = ''"
            ).fetchone()[0]
        )
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise BoardInventoryError(f"board inventory failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    return BoardSnapshot(
        board=board,
        titles={row["id"]: row["title"] for row in candidates},
        assignees={row["id"]: row["assignee"] for row in candidates},
        statuses={row["id"]: row["status"] for row in candidates},
        runs=runs,
        unowned_subscriptions=unowned,
    )


def _validate_schema(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row["name"] for row in rows}
        missing = required - columns
        if missing:
            raise BoardInventoryError(
                f"schema mismatch for {table}: missing {', '.join(sorted(missing))}"
            )


def _canonical_run(row: sqlite3.Row, board: str) -> CanonicalRun:
    required = ("worker_pid", "worker_started_at", "current_run_id", "run_id", "run_task_id")
    if any(row[key] is None for key in required):
        raise BoardInventoryError(f"incomplete active identity for {row['task_id']}")
    if row["run_id"] != row["current_run_id"] or row["run_task_id"] != row["task_id"]:
        raise BoardInventoryError(f"active run mismatch for {row['task_id']}")
    profile = row["profile"] or row["assignee"]
    if not isinstance(profile, str) or not profile:
        raise BoardInventoryError(f"active run profile missing for {row['task_id']}")
    return CanonicalRun(
        board=board,
        task_id=row["task_id"],
        run_id=int(row["run_id"]),
        pid=int(row["worker_pid"]),
        worker_started_at=int(row["worker_started_at"]),
        profile=profile,
        task_status=row["task_status"],
    )
