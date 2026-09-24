"""Contract tests pinned to the real `hermes kanban dispatch --json` shape.

The payload below is the verbatim key set emitted by the retained Hermes
source (hermes_cli/kanban_ops.py::_cmd_dispatch, present since ec64ec0d24c and
unchanged at the pinned baseline). The earlier fixtures used an older 11-key
shape, so every prediction against the live CLI failed closed with
"dispatch output contract mismatch" and the helper could never admit work.
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from resource_controller.cli_adapter import CliCommands
from resource_controller.cli_contract import (
    DISPATCH_KEYS,
    CommandContractError,
    parse_dispatch_prediction,
)
from resource_controller.engine import BoardView
from resource_controller.priority import PredictedPick
from resource_controller.supervision import CommandResult

# Captured from the live CLI on 2026-09-24 (`--board default dispatch --dry-run
# --max 1 --failure-limit 2 --json`), with one predicted spawn substituted.
LIVE_PAYLOAD = {
    "reclaimed": 0,
    "crashed": [],
    "timed_out": [],
    "stale": [],
    "auto_blocked": [],
    "promoted": 0,
    "reaped_terminal_workers": [],
    "spawned": [{"task_id": "t_1", "assignee": "builder", "workspace": ""}],
    "skipped_unassigned": [],
    "skipped_nonspawnable": [],
    "skipped_per_profile_capped": [],
    "auto_assigned_default": [],
    "respawn_guarded": [],
    "rate_limited": [],
    "skipped_locked": False,
    "memory_pressure": None,
}


def _with(**changes):
    payload = json.loads(json.dumps(LIVE_PAYLOAD))
    payload.update(changes)
    return json.dumps(payload)


class LiveDispatchContractTests(unittest.TestCase):
    def test_key_set_matches_live_cli_exactly(self) -> None:
        self.assertEqual(DISPATCH_KEYS, frozenset(LIVE_PAYLOAD))

    def test_live_shape_prediction_parses(self) -> None:
        pick = parse_dispatch_prediction(json.dumps(LIVE_PAYLOAD), board="default", titles={"t_1": "Build"})
        self.assertEqual(pick, PredictedPick("default", "t_1", "builder", "Build"))

    def test_legacy_eleven_key_shape_is_drift(self) -> None:
        legacy = {k: v for k, v in LIVE_PAYLOAD.items() if k not in {
            "reaped_terminal_workers", "respawn_guarded", "rate_limited", "skipped_locked", "memory_pressure"}}
        with self.assertRaisesRegex(CommandContractError, "contract mismatch"):
            parse_dispatch_prediction(json.dumps(legacy), board="default", titles={"t_1": "Build"})

    def test_unknown_extra_key_is_drift(self) -> None:
        with self.assertRaisesRegex(CommandContractError, "contract mismatch"):
            parse_dispatch_prediction(_with(new_field=[]), board="default", titles={"t_1": "Build"})

    def test_new_maintenance_fields_block_prediction(self) -> None:
        for changes in (
            {"reaped_terminal_workers": ["t_old"]},
            {"rate_limited": ["t_rl"]},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(CommandContractError, "maintenance"):
                    parse_dispatch_prediction(_with(**changes), board="default", titles={"t_1": "Build"})

    def test_contended_dispatch_lock_is_refused(self) -> None:
        with self.assertRaisesRegex(CommandContractError, "lock"):
            parse_dispatch_prediction(_with(skipped_locked=True), board="default", titles={"t_1": "Build"})

    def test_any_reported_memory_pressure_refuses(self) -> None:
        # The helper's own Darwin pressure gate is authoritative; any Hermes-side
        # pressure signal (elevated, critical, or unknown text) is fail-closed.
        for value in ("elevated", "critical", "bogus"):
            with self.subTest(memory_pressure=value):
                with self.assertRaisesRegex(CommandContractError, "pressure"):
                    parse_dispatch_prediction(_with(memory_pressure=value), board="default", titles={"t_1": "Build"})
        for bad in (3, []):
            with self.subTest(memory_pressure=bad):
                with self.assertRaisesRegex(CommandContractError, "wrong type"):
                    parse_dispatch_prediction(_with(memory_pressure=bad), board="default", titles={"t_1": "Build"})

    def test_field_types_are_enforced(self) -> None:
        for changes in (
            {"skipped_locked": 0},
            {"respawn_guarded": {}},
            {"rate_limited": None},
            {"reaped_terminal_workers": "t_x"},
            {"reclaimed": False},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(CommandContractError):
                    parse_dispatch_prediction(_with(**changes), board="default", titles={"t_1": "Build"})

    def test_respawn_guarded_rows_do_not_block_prediction(self) -> None:
        guarded = [{"task_id": "t_g", "reason": "active_pr"}]
        pick = parse_dispatch_prediction(_with(respawn_guarded=guarded), board="default", titles={"t_1": "Build"})
        self.assertEqual(pick.task_id, "t_1")


class LiveDispatchAdapterTests(unittest.TestCase):
    def _adapter(self, stdout: str, actual: str | None = "t_1") -> CliCommands:
        def runner(argv: list[str], timeout: float, limit: int) -> CommandResult:
            return CommandResult(tuple(argv), 0, stdout, "", False, False)

        return CliCommands(Path("/opt/hermes/bin/hermes"), runner=runner,
                           reconcile_actual=lambda _board, _pred: actual)

    def test_live_shape_dispatch_is_certain(self) -> None:
        outcome = self._adapter(json.dumps(LIVE_PAYLOAD)).dispatch(
            PredictedPick("default", "t_1", "builder", "Build"), 2)
        self.assertFalse(outcome.uncertain)
        self.assertEqual(outcome.actual_task_id, "t_1")

    def test_legacy_shape_dispatch_is_uncertain(self) -> None:
        legacy = {k: v for k, v in LIVE_PAYLOAD.items() if k != "memory_pressure"}
        outcome = self._adapter(json.dumps(legacy)).dispatch(
            PredictedPick("default", "t_1", "builder", "Build"), 2)
        self.assertTrue(outcome.uncertain)

    def test_lock_skipped_dispatch_is_uncertain(self) -> None:
        # Another dispatcher owned the board tick: dual authority, never a clean miss.
        outcome = self._adapter(_with(skipped_locked=True, spawned=[]), actual=None).dispatch(
            PredictedPick("default", "t_1", "builder", "Build"), 2)
        self.assertTrue(outcome.uncertain)


if __name__ == "__main__":
    unittest.main()
