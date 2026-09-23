from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from resource_controller.config import ControllerConfig
from resource_controller.engine import (
    BoardView,
    CommandOutcome,
    ControllerEngine,
    GateSnapshot,
)
from resource_controller.policy import AdmissionPolicy, HostSample
from resource_controller.priority import PredictedPick
from resource_controller.storage import SecureStateStore

GIB = 1024**3


def config(auto_decompose: bool = False) -> ControllerConfig:
    return ControllerConfig.from_mapping({
        "dispatch_in_gateway": False,
        "max_in_progress": 2,
        "max_in_progress_per_profile": 1,
        "failure_limit": 2,
        "auto_decompose": auto_decompose,
        "reconcile_orphans": True,
        "dispatch_stale_timeout_seconds": 0,
        "review_dispatch": True,
        "default_assignee": None,
        "dispatch_profiles": None,
    })


def host(now: float) -> HostSample:
    return HostSample(now, 1, 10, 6 * GIB, "normal", 100, 100)


def snapshot(
    now: float,
    *,
    fingerprint: str = "same",
    estop: bytes | None = None,
    manual_hold: bool = False,
    boards: tuple[BoardView, ...] = (),
    auto_decompose: bool = False,
) -> GateSnapshot:
    return GateSnapshot(
        fingerprint=fingerprint,
        sample=host(now),
        config=config(auto_decompose),
        estop=estop,
        manual_hold=manual_hold,
        workers=(),
        boards=boards,
        unowned_subscriptions=0,
    )


class FakeWorld:
    def __init__(self, states: list[GateSnapshot]) -> None:
        self.states = states
        self.index = 0

    def capture(self) -> GateSnapshot:
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return state


class FakeCommands:
    def __init__(self) -> None:
        self.predictions: dict[str, PredictedPick | Exception] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.outcome = CommandOutcome(False, "t_actual")

    def predict(self, board: BoardView, failure_limit: int) -> PredictedPick:
        value = self.predictions[board.board]
        if isinstance(value, Exception):
            raise value
        return value

    def dispatch(self, pick: PredictedPick, failure_limit: int) -> CommandOutcome:
        self.calls.append(("dispatch", pick.board, pick.task_id))
        return self.outcome

    def decompose(self, board: str, task_id: str) -> CommandOutcome:
        self.calls.append(("decompose", board, task_id))
        return self.outcome


class EngineTests(unittest.TestCase):
    def make_engine(self, root: str, world: FakeWorld, commands: FakeCommands) -> ControllerEngine:
        policy = AdmissionPolicy(recovery_seconds=120, max_sample_gap=35)
        for now in (0, 30, 60, 90):
            policy.observe(host(now))
        return ControllerEngine(policy, SecureStateStore(Path(root) / "state"), world, commands)

    def test_estop_is_read_only_and_prevents_every_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([snapshot(120, estop=b'{"reason":"manual"}')])
            commands = FakeCommands()
            result = self.make_engine(root, world, commands).tick()
            self.assertEqual(result.reason, "estop")
            self.assertEqual(commands.calls, [])
            self.assertEqual(world.states[0].estop, b'{"reason":"manual"}')

    def test_decomposition_consumes_window_without_dispatch(self) -> None:
        board = BoardView("a", {"t_triage": "Specify"}, {"t_triage": "specifier"}, "t_triage")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([snapshot(120, boards=(board,), auto_decompose=True), snapshot(121, boards=(board,), auto_decompose=True)])
            commands = FakeCommands()
            commands.outcome = CommandOutcome(False, "t_triage")
            result = self.make_engine(root, world, commands).tick()
            self.assertEqual(result.reason, "decomposed")
            self.assertEqual(commands.calls, [("decompose", "a", "t_triage")])

    def test_highest_stage_dispatches_once_and_logs_priority_miss(self) -> None:
        boards = (
            BoardView("build", {"t_b": "Build"}, {"t_b": "builder"}, None),
            BoardView("review", {"t_r": "Review"}, {"t_r": "reviewquality"}, None),
        )
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([snapshot(120, boards=boards), snapshot(121, boards=boards)])
            commands = FakeCommands()
            commands.predictions = {
                "build": PredictedPick("build", "t_b", "builder", "Build"),
                "review": PredictedPick("review", "t_r", "reviewquality", "Review"),
            }
            commands.outcome = CommandOutcome(False, "t_other")
            engine = self.make_engine(root, world, commands)
            result = engine.tick()
            self.assertEqual(commands.calls, [("dispatch", "review", "t_r")])
            self.assertEqual(result.reason, "priority-miss")
            self.assertEqual(result.priority_misses, 1)

    def test_precommand_fingerprint_change_prevents_side_effect(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([snapshot(120, fingerprint="one", boards=(board,)), snapshot(121, fingerprint="two", boards=(board,))])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            engine = self.make_engine(root, world, commands)
            result = engine.tick()
            self.assertEqual(result.reason, "precommand-race")
            self.assertEqual(commands.calls, [])
            self.assertEqual(engine.passed, {}, "aborted windows must not count as passed admissions")

    def test_no_eligible_prediction_advances_round_robin_pointer(self) -> None:
        boards = (
            BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None),
            BoardView("b", {"t_2": "Build"}, {"t_2": "builder"}, None),
        )
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([snapshot(120, boards=boards)])
            commands = FakeCommands()
            commands.predictions = {"a": ValueError("bad"), "b": ValueError("bad")}
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "prediction-unavailable")
            self.assertEqual(engine.pointer, 1)

    def test_uncertain_command_persists_and_blocks_restart(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        with tempfile.TemporaryDirectory() as root:
            states = [snapshot(120, boards=(board,)), snapshot(121, boards=(board,))]
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            commands.outcome = CommandOutcome(True, None)
            first = self.make_engine(root, FakeWorld(states), commands).tick()
            self.assertEqual(first.reason, "uncertain-outcome")

            restarted_commands = FakeCommands()
            restarted = self.make_engine(root, FakeWorld([snapshot(120, boards=(board,))]), restarted_commands).tick()
            self.assertEqual(restarted.reason, "uncertain-outcome")
            self.assertEqual(restarted_commands.calls, [])


if __name__ == "__main__":
    unittest.main()
