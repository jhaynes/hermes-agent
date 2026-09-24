from __future__ import annotations

import json
from pathlib import Path
import unittest

from resource_controller.cli_adapter import CliCommands
from resource_controller.engine import BoardView
from resource_controller.priority import PredictedPick
from resource_controller.supervision import CommandResult


class CliAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float, int]] = []
        self.actual = "t_1"

        def runner(argv: list[str], timeout: float, limit: int) -> CommandResult:
            self.calls.append((tuple(argv), timeout, limit))
            if "--dry-run" in argv:
                payload = {
                    "reclaimed": 0, "crashed": [], "timed_out": [], "stale": [],
                    "auto_blocked": [], "promoted": 0,
                    "spawned": [{"task_id": "t_1", "assignee": "reviewscope", "workspace": None}],
                    "skipped_unassigned": [], "skipped_nonspawnable": [],
                    "skipped_per_profile_capped": [], "auto_assigned_default": [],
            "reaped_terminal_workers": [], "respawn_guarded": [], "rate_limited": [], "skipped_locked": False, "memory_pressure": None,
                }
            elif "decompose" in argv:
                payload = {
                    "task_id": "t_triage", "ok": True, "reason": "",
                    "fanout": True, "child_ids": ["t_child"], "new_title": None,
                }
            else:
                payload = {
                    "reclaimed": 0, "crashed": [], "timed_out": [], "stale": [],
                    "auto_blocked": [], "promoted": 0,
                    "spawned": [{"task_id": "t_1", "assignee": "reviewscope", "workspace": None}],
                    "skipped_unassigned": [], "skipped_nonspawnable": [],
                    "skipped_per_profile_capped": [], "auto_assigned_default": [],
            "reaped_terminal_workers": [], "respawn_guarded": [], "rate_limited": [], "skipped_locked": False, "memory_pressure": None,
                }
            return CommandResult(tuple(argv), 0, json.dumps(payload), "", False, False)

        self.adapter = CliCommands(
            Path("/opt/hermes/bin/hermes"),
            runner=runner,
            reconcile_actual=lambda _board, _before: self.actual,
        )

    def test_prediction_is_read_only_bounded_and_parsed(self) -> None:
        board = BoardView("alpha", {"t_1": "Review"}, {"t_1": "reviewscope"}, None)
        pick = self.adapter.predict(board, 2)
        self.assertEqual(pick, PredictedPick("alpha", "t_1", "reviewscope", "Review"))
        self.assertIn("--dry-run", self.calls[0][0])
        self.assertEqual(self.calls[0][1:], (15.0, 65536))

    def test_dispatch_is_one_command_and_uses_post_reconciliation(self) -> None:
        pick = PredictedPick("alpha", "t_1", "reviewscope", "Review")
        outcome = self.adapter.dispatch(pick, 2)
        self.assertFalse(outcome.uncertain)
        self.assertEqual(outcome.actual_task_id, "t_1")
        self.assertNotIn("--dry-run", self.calls[0][0])
        self.assertEqual(self.calls[0][1], 30.0)

    def test_timeout_or_bad_decomposition_is_uncertain(self) -> None:
        def timed(argv: list[str], timeout: float, limit: int) -> CommandResult:
            return CommandResult(tuple(argv), 0, "{}", "", True, False)

        adapter = CliCommands(
            Path("/opt/hermes/bin/hermes"),
            runner=timed,
            reconcile_actual=lambda _board, _before: None,
        )
        self.assertTrue(adapter.dispatch(PredictedPick("a", "t_1", "builder", "Build"), 2).uncertain)
        self.assertTrue(adapter.decompose("a", "t_triage").uncertain)

    def test_decomposition_requires_requested_task_and_confirmed_success(self) -> None:
        self.actual = None
        outcome = self.adapter.decompose("alpha", "t_triage")
        self.assertFalse(outcome.uncertain)
        self.assertEqual(outcome.actual_task_id, "t_triage")
        self.assertNotIn("--all", self.calls[0][0])
        self.actual = "t_manual"
        self.assertTrue(self.adapter.decompose("alpha", "t_triage").uncertain)


if __name__ == "__main__":
    unittest.main()
