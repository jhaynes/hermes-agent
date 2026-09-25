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
        pick = self.adapter.predict(board, 2, 3)
        self.assertEqual(pick, PredictedPick("alpha", "t_1", "reviewscope", "Review"))
        self.assertIn("--dry-run", self.calls[0][0])
        self.assertEqual(self.calls[0][1:], (15.0, 65536))

    def test_dispatch_is_one_command_and_uses_post_reconciliation(self) -> None:
        pick = PredictedPick("alpha", "t_1", "reviewscope", "Review")
        outcome = self.adapter.dispatch(pick, 2, 3)
        self.assertFalse(outcome.uncertain)
        self.assertEqual(outcome.actual_task_id, "t_1")
        self.assertNotIn("--dry-run", self.calls[0][0])
        self.assertEqual(self.calls[0][0][self.calls[0][0].index("--max") + 1], "3")
        self.assertEqual(self.calls[0][1], 30.0)

    def test_multi_start_claims_are_retained_and_invalid_claims_are_uncertain(self) -> None:
        base = {
            "reclaimed": 0, "crashed": [], "timed_out": [], "stale": [],
            "auto_blocked": [], "promoted": 0, "skipped_unassigned": [],
            "skipped_nonspawnable": [], "skipped_per_profile_capped": [],
            "auto_assigned_default": [], "reaped_terminal_workers": [],
            "respawn_guarded": [], "rate_limited": [], "skipped_locked": False,
            "memory_pressure": None,
        }

        def adapter(spawned) -> CliCommands:
            payload = {**base, "spawned": spawned}
            return CliCommands(
                Path("/opt/hermes/bin/hermes"),
                runner=lambda argv, timeout, limit: CommandResult(
                    tuple(argv), 0, json.dumps(payload), "", False, False,
                ),
                reconcile_actual=lambda _board, _before: None,
            )

        rows = [
            {"task_id": "t_1", "assignee": "builder", "workspace": None},
            {"task_id": "t_2", "assignee": "builder", "workspace": None},
        ]
        pick = PredictedPick("a", "t_1", "builder", "Build")
        outcome = adapter(rows).dispatch(pick, 2, 2)
        self.assertEqual(outcome.spawned, (("t_1", "builder"), ("t_2", "builder")))
        self.assertFalse(outcome.uncertain)
        self.assertTrue(adapter(rows).dispatch(pick, 2, 1).uncertain)
        duplicate = adapter([rows[0], rows[0]]).dispatch(pick, 2, 2)
        malformed = adapter([{"task_id": "t_1"}]).dispatch(pick, 2, 1)
        self.assertEqual(duplicate.error.type, "ContractDrift")
        self.assertEqual(malformed.error.type, "ContractDrift")

    def test_timeout_or_bad_decomposition_is_uncertain(self) -> None:
        def timed(argv: list[str], timeout: float, limit: int) -> CommandResult:
            return CommandResult(tuple(argv), 0, "{}", "", True, False)

        adapter = CliCommands(
            Path("/opt/hermes/bin/hermes"),
            runner=timed,
            reconcile_actual=lambda _board, _before: None,
        )
        dispatch = adapter.dispatch(PredictedPick("a", "t_1", "builder", "Build"), 2, 1)
        decompose = adapter.decompose("a", "t_triage")
        self.assertEqual(dispatch.error.type, "CommandTimeout")
        self.assertEqual(decompose.error.type, "CommandTimeout")
        self.assertNotIn("{}", decompose.error.message)

    def test_decomposition_requires_requested_task_and_confirmed_success(self) -> None:
        self.actual = None
        outcome = self.adapter.decompose("alpha", "t_triage")
        self.assertFalse(outcome.uncertain)
        self.assertEqual(outcome.actual_task_id, "t_triage")
        self.assertNotIn("--all", self.calls[0][0])
        self.actual = "t_manual"
        uncertain = self.adapter.decompose("alpha", "t_triage")
        self.assertTrue(uncertain.uncertain)
        self.assertEqual(uncertain.error.type, "AmbiguousReconciliation")


if __name__ == "__main__":
    unittest.main()
