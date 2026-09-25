from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .board_inventory import read_board_inventory
from .config import ControllerConfig
from .engine import BoardView, GateSnapshot
from .inventory import LiveWorker, ProcessSnapshot, reconcile_workers
from .policy import HostSample
from .spec import AdmissionCaps
from .worker_identity import current_instantiation_epoch


class RuntimeWorld:
    """Fresh, read-only gate snapshot assembled for every decision fence."""

    def __init__(
        self,
        *,
        boards: Mapping[str, Path],
        admission_caps: AdmissionCaps,
        sampler: Callable[[], HostSample],
        config_reader: Callable[[], Mapping[str, object]],
        process_reader: Callable[[], Sequence[ProcessSnapshot]],
        estop_paths: Sequence[Path],
        manual_hold: Callable[[], bool],
        max_rows: int = 10_000,
    ) -> None:
        if not boards:
            raise ValueError("at least one board is required")
        self._boards = dict(boards)
        self._admission_caps = admission_caps
        self._sampler = sampler
        self._config_reader = config_reader
        self._process_reader = process_reader
        self._estop_paths = tuple(estop_paths)
        self._manual_hold = manual_hold
        self._max_rows = max_rows
        self._previous_workers: tuple[LiveWorker, ...] = ()

    def capture(self) -> GateSnapshot:
        config = ControllerConfig.from_mapping(
            self._config_reader(), admission_caps=self._admission_caps,
        )
        sample = self._sampler()
        snapshots = [
            read_board_inventory(
                self._boards[slug],
                board=slug,
                max_rows=self._max_rows,
                ended_candidates=tuple(
                    worker for worker in self._previous_workers if worker.board == slug
                ),
            )
            for slug in sorted(self._boards)
        ]
        runs = tuple(run for snapshot in snapshots for run in snapshot.runs)
        ended_runs = tuple(run for snapshot in snapshots for run in snapshot.ended_runs)
        epoch = current_instantiation_epoch()
        workers = tuple(reconcile_workers(
            runs,
            self._process_reader(),
            epoch=epoch,
            ended_runs=ended_runs,
            previous_workers=self._previous_workers,
        ))
        boards = tuple(
            BoardView(
                snapshot.board,
                snapshot.titles,
                snapshot.assignees,
                next(
                    (task_id for task_id, status in snapshot.statuses.items() if status == "triage"),
                    None,
                ),
                snapshot.hermes_db_running_count,
                config.dispatch_profiles,
            )
            for snapshot in snapshots
        )
        estop = self._read_estop()
        manual = bool(self._manual_hold())
        unowned = sum(snapshot.unowned_subscriptions for snapshot in snapshots)
        fingerprint = _fingerprint(config, estop, manual, snapshots, workers)
        captured = GateSnapshot(
            fingerprint,
            sample,
            config,
            estop,
            manual,
            workers,
            boards,
            unowned,
        )
        self._previous_workers = workers
        return captured

    def _read_estop(self) -> bytes | None:
        engaged: list[bytes] = []
        for path in self._estop_paths:
            try:
                if not path.exists():
                    continue
                if path.is_symlink() or not path.is_file():
                    engaged.append(f"unreadable:{path}".encode())
                    continue
                with path.open("rb") as handle:
                    value = handle.read(65537)
                if len(value) > 65536:
                    engaged.append(f"oversized:{path}".encode())
                else:
                    engaged.append(value)
            except OSError:
                engaged.append(f"unreadable:{path}".encode())
        return b"\n".join(engaged) if engaged else None


def _fingerprint(config, estop, manual, boards, workers) -> str:
    payload = {
        "config": asdict(config),
        "estop_sha256": None if estop is None else hashlib.sha256(estop).hexdigest(),
        "manual_hold": manual,
        "boards": [
            {
                "board": board.board,
                "titles": dict(board.titles),
                "assignees": dict(board.assignees),
                "statuses": dict(board.statuses),
                "runs": [asdict(run) for run in board.runs],
                "ended_runs": [asdict(run) for run in board.ended_runs],
                "hermes_db_running_count": board.hermes_db_running_count,
                "unowned": board.unowned_subscriptions,
            }
            for board in boards
        ],
        "workers": [asdict(worker) for worker in workers],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
