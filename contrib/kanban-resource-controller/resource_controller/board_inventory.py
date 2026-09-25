from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
from typing import Mapping, Sequence
from urllib.parse import quote

from .inventory import CanonicalRun, LiveWorker
from .worker_identity import (
    IdentityHold,
    StoredFingerprint,
    parse_stored_fingerprint,
    require_consistent,
)


class BoardInventoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class EndedRunIdentity:
    board: str
    task_id: str
    run_id: int
    profile: str
    pid: int
    worker_fingerprint: StoredFingerprint
    run_status: str
    ended_at: int


@dataclass(frozen=True)
class BoardSnapshot:
    board: str
    titles: Mapping[str, str]
    assignees: Mapping[str, str | None]
    statuses: Mapping[str, str]
    runs: tuple[CanonicalRun, ...]
    ended_runs: tuple[EndedRunIdentity, ...]
    hermes_db_running_count: int
    unowned_subscriptions: int


_REQUIRED_COLUMNS = {
    "tasks": {
        "id", "title", "assignee", "status", "priority", "created_at",
        "worker_pid", "worker_started_at", "current_run_id"
    },
    "task_runs": {
        "id", "task_id", "profile", "status", "worker_pid", "worker_started_at", "ended_at",
    },
    "kanban_notify_subs": {"task_id", "notifier_profile"},
}


def read_board_inventory(
    path: Path,
    *,
    board: str,
    max_rows: int = 10_000,
    ended_candidates: Sequence[LiveWorker] = (),
) -> BoardSnapshot:
    if max_rows <= 0:
        raise BoardInventoryError("row bound must be positive")
    candidate_keys = [
        (item.board, item.task_id, item.run_id, item.pid, item.profile)
        for item in ended_candidates
    ]
    if len(candidate_keys) > max_rows:
        raise BoardInventoryError("ended candidate row bound exceeded")
    if len(set(candidate_keys)) != len(candidate_keys):
        raise BoardInventoryError("duplicate ended candidate")
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
            "r.task_id AS run_task_id, r.profile, r.worker_pid AS run_worker_pid, "
            "r.worker_started_at AS run_worker_started_at "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.worker_pid IS NOT NULL"
        ).fetchall()
        if len(run_rows) > max_rows:
            raise BoardInventoryError("active run row bound exceeded")
        runs = tuple(_canonical_run(row, board) for row in run_rows)
        ended_runs = tuple(
            ended
            for candidate in ended_candidates
            if candidate.board == board
            for ended in _ended_identity(connection, board, candidate)
        )
        hermes_db_running_count = int(
            connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
        )
        unowned = int(
            connection.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs "
                "WHERE notifier_profile IS NULL OR TRIM(notifier_profile) = ''"
            ).fetchone()[0]
        )
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise BoardInventoryError(f"board inventory failed: {exc}") from exc
    except IdentityHold as exc:
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
        ended_runs=ended_runs,
        hermes_db_running_count=hermes_db_running_count,
        unowned_subscriptions=unowned,
    )


def _ended_identity(
    connection: sqlite3.Connection,
    board: str,
    candidate: LiveWorker,
) -> tuple[EndedRunIdentity, ...]:
    rows = connection.execute(
        "SELECT id, task_id, profile, status, worker_pid, worker_started_at, ended_at "
        "FROM task_runs WHERE id = ? AND task_id = ? AND worker_pid = ? "
        "AND worker_started_at IS NOT NULL AND ended_at IS NOT NULL",
        (candidate.run_id, candidate.task_id, candidate.pid),
    ).fetchall()
    if not rows:
        return ()
    if len(rows) != 1:
        raise BoardInventoryError(f"duplicate ended identity for {candidate.task_id}/{candidate.run_id}")
    row = rows[0]
    if row["profile"] != candidate.profile or row["worker_started_at"] != candidate.worker_fingerprint:
        raise BoardInventoryError(f"ended identity mismatch for {candidate.task_id}/{candidate.run_id}")
    try:
        fingerprint = parse_stored_fingerprint(
            row["worker_started_at"], context=f"{candidate.task_id}/{candidate.run_id}",
        )
    except IdentityHold as exc:
        raise BoardInventoryError(f"ended identity invalid for {candidate.task_id}/{candidate.run_id}: {exc}") from exc
    return (EndedRunIdentity(
        board=board,
        task_id=row["task_id"],
        run_id=int(row["id"]),
        profile=row["profile"],
        pid=int(row["worker_pid"]),
        worker_fingerprint=fingerprint,
        run_status=row["status"],
        ended_at=int(row["ended_at"]),
    ),)


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
    required = (
        "worker_pid", "worker_started_at", "current_run_id", "run_id", "run_task_id",
        "run_worker_pid", "run_worker_started_at",
    )
    if any(row[key] is None for key in required):
        raise BoardInventoryError(f"incomplete active identity for {row['task_id']}")
    if row["run_id"] != row["current_run_id"] or row["run_task_id"] != row["task_id"]:
        raise BoardInventoryError(f"active run mismatch for {row['task_id']}")
    profile = row["profile"] or row["assignee"]
    if not isinstance(profile, str) or not profile:
        raise BoardInventoryError(f"active run profile missing for {row['task_id']}")
    fingerprint = require_consistent(
        row["worker_started_at"], row["worker_pid"],
        row["run_worker_started_at"], row["run_worker_pid"],
        context=f"{row['task_id']}/{row['run_id']}",
    )
    return CanonicalRun(
        board=board,
        task_id=row["task_id"],
        run_id=int(row["run_id"]),
        pid=int(row["worker_pid"]),
        worker_fingerprint=fingerprint,
        profile=profile,
        task_status=row["task_status"],
    )
