from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import re
from typing import Mapping, Sequence


class Stage(IntEnum):
    BUILD = 1
    REVIEW = 2
    MERGE_CONFLICT = 3


_MERGE_PATTERN = re.compile(r"conflict|merge|rebase", re.IGNORECASE)


def classify_stage(assignee: str | None, title: str) -> Stage:
    normalized = assignee or ""
    if normalized.startswith("builder") and _MERGE_PATTERN.search(title):
        return Stage.MERGE_CONFLICT
    if normalized.startswith("review"):
        return Stage.REVIEW
    return Stage.BUILD


@dataclass(frozen=True)
class PredictedPick:
    board: str
    task_id: str
    assignee: str | None
    title: str

    def __post_init__(self) -> None:
        if not self.board or not self.task_id or not self.title:
            raise ValueError("prediction requires board, task id, and title")

    @property
    def stage(self) -> Stage:
        return classify_stage(self.assignee, self.title)


@dataclass(frozen=True)
class Selection:
    pick: PredictedPick
    next_pointer: int
    passed: dict[str, int]


def select_pick(
    picks: Sequence[PredictedPick],
    *,
    board_order: Sequence[str],
    pointer: int,
    passed: Mapping[str, int],
) -> Selection:
    if not picks:
        raise ValueError("at least one eligible prediction is required")
    if not board_order or len(set(board_order)) != len(board_order):
        raise ValueError("board order must be unique and nonempty")
    by_board = {pick.board: pick for pick in picks}
    if len(by_board) != len(picks) or any(board not in board_order for board in by_board):
        raise ValueError("predictions must map uniquely to known boards")

    rotated = list(board_order[pointer:]) + list(board_order[:pointer])
    aged = [by_board[board] for board in rotated if board in by_board and passed.get(board, 0) >= 6]
    if aged:
        chosen = aged[0]
    else:
        highest = max(pick.stage for pick in picks)
        chosen = next(by_board[board] for board in rotated if board in by_board and by_board[board].stage == highest)

    updated = dict(passed)
    for pick in picks:
        updated[pick.board] = 0 if pick.board == chosen.board else updated.get(pick.board, 0) + 1
    selected_index = list(board_order).index(chosen.board)
    return Selection(chosen, (selected_index + 1) % len(board_order), updated)
