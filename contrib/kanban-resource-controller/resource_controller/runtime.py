from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .board_inventory import read_board_inventory
from .config import ControllerConfig
from .engine import BoardView, GateSnapshot
from .inventory import ProcessSnapshot, reconcile_workers
from .policy import HostSample


class RuntimeWorld:
    """Fresh, read-only gate snapshot assembled for every decision fence."""

    def __init__(
        self,
        *,
        boards: Mapping[str, Path],
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
        self._sampler = sampler
        self._config_reader = config_reader
        self._process_reader = process_reader
        self._estop_paths = tuple(estop_paths)
        self._manual_hold = manual_hold
        self._max_rows = max_rows

    def capture(self) -> GateSnapshot:
        config = ControllerConfig.from_mapping(self._config_reader())
        sample = self._sampler()
        snapshots = [
            read_board_inventory(self._boards[slug], board=slug, max_rows=self._max_rows)
            for slug in sorted(self._boards)
        ]
        runs = tuple(run for snapshot in snapshots for run in snapshot.runs)
        workers = tuple(reconcile_workers(runs, self._process_reader()))
        boards = tuple(
            BoardView(
                snapshot.board,
                snapshot.titles,
                snapshot.assignees,
                next(
                    (task_id for task_id, status in snapshot.statuses.items() if status == "triage"),
                    None,
                ),
            )
            for snapshot in snapshots
        )
        estop = self._read_estop()
        manual = bool(self._manual_hold())
        unowned = sum(snapshot.unowned_subscriptions for snapshot in snapshots)
        fingerprint = _fingerprint(config, estop, manual, snapshots, workers)
        return GateSnapshot(
            fingerprint,
            sample,
            config,
            estop,
            manual,
            workers,
            boards,
            unowned,
        )

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
                "unowned": board.unowned_subscriptions,
            }
            for board in boards
        ],
        "workers": [asdict(worker) for worker in workers],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
