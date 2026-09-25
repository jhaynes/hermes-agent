from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from resource_controller.config import ControllerConfig
from resource_controller.cli_contract import PrecommandRace
from resource_controller.engine import (
    BoardView,
    CommandOutcome,
    ControllerEngine,
    ErrorDetail,
    GateSnapshot,
)
from resource_controller.inventory import LiveWorker, ProcessSnapshot
from resource_controller.policy import AdmissionPolicy, HostSample
from resource_controller.priority import PredictedPick
from resource_controller.runtime import RuntimeWorld
from resource_controller.storage import SecureStateStore
from resource_controller.spec import AdmissionCaps

GIB = 1024**3
DEFAULT_CAPS = AdmissionCaps(2, 1, (), 1, ())


def config(
    auto_decompose: bool = False,
    caps: AdmissionCaps = DEFAULT_CAPS,
) -> ControllerConfig:
    return ControllerConfig.from_mapping({
        "dispatch_in_gateway": False,
        "max_in_progress": caps.host_cap,
        "max_in_progress_per_profile": caps.maximum_profile_cap,
        "failure_limit": 2,
        "auto_decompose": auto_decompose,
        "reconcile_orphans": True,
        "dispatch_stale_timeout_seconds": 0,
        "review_dispatch": True,
        "default_assignee": None,
        "dispatch_profiles": [
            "builder", "reviewquality", "reviewscope", "reviewer", "default", "p1", "p2", "p3", "p4",
        ],
    }, admission_caps=caps)


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
    workers: tuple[LiveWorker, ...] = (),
    unowned_subscriptions: int = 0,
    caps: AdmissionCaps = DEFAULT_CAPS,
) -> GateSnapshot:
    return GateSnapshot(
        fingerprint=fingerprint,
        sample=host(now),
        config=config(auto_decompose, caps),
        estop=estop,
        manual_hold=manual_hold,
        workers=workers,
        boards=boards,
        unowned_subscriptions=unowned_subscriptions,
    )


class FakeWorld:
    def __init__(self, states: list[GateSnapshot | BaseException]) -> None:
        self.states = states
        self.index = 0

    def capture(self) -> GateSnapshot:
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        if isinstance(state, BaseException):
            raise state
        return state


class FakeCommands:
    def __init__(self) -> None:
        self.predictions: dict[str, PredictedPick | Exception] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.outcome = CommandOutcome(False)
        self.dispatch_maxes: list[int] = []

    def predict(self, board: BoardView, failure_limit: int, dispatch_max: int) -> PredictedPick:
        self.dispatch_maxes.append(dispatch_max)
        value = self.predictions[board.board]
        if isinstance(value, Exception):
            raise value
        return value

    def dispatch(self, pick: PredictedPick, failure_limit: int, dispatch_max: int) -> CommandOutcome:
        self.dispatch_maxes.append(dispatch_max)
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

    def test_nondefault_common_host_cap_uses_the_shared_caps_object(self) -> None:
        caps = AdmissionCaps(4, 2, (), 4, ())
        board = BoardView("a", {}, {}, None)
        workers = tuple(
            LiveWorker(f"b{index}", f"t_{index}", index, index, float(index), f"p{index}", "running")
            for index in range(4)
        )
        with tempfile.TemporaryDirectory() as root:
            engine = self.make_engine(root, FakeWorld([]), FakeCommands())
            self.assertIsNone(engine._common_hold(snapshot(120, boards=(board,), workers=workers[:3], caps=caps)))
            self.assertEqual(
                engine._common_hold(snapshot(120, boards=(board,), workers=workers, caps=caps)),
                "host-cap",
            )

    def test_decomposition_consumes_window_without_dispatch(self) -> None:
        board = BoardView("a", {"t_triage": "Specify"}, {"t_triage": "specifier"}, "t_triage")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([snapshot(120, boards=(board,), auto_decompose=True), snapshot(121, boards=(board,), auto_decompose=True)])
            commands = FakeCommands()
            commands.outcome = CommandOutcome(False, ("t_triage",))
            result = self.make_engine(root, world, commands).tick()
            self.assertEqual(result.reason, "decomposed")
            self.assertEqual(commands.calls, [("decompose", "a", "t_triage")])

    def test_highest_stage_dispatches_once_and_logs_priority_miss(self) -> None:
        boards = (
            BoardView("build", {"t_b": "Build"}, {"t_b": "builder"}, None),
            BoardView(
                "review",
                {"t_r": "Review", "t_other": "Other review"},
                {"t_r": "reviewquality", "t_other": "reviewquality"},
                None,
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            actual = LiveWorker("review", "t_other", 9, 42, 1.0, "reviewquality", "running")
            world = FakeWorld([
                snapshot(120, boards=boards),
                snapshot(121, boards=boards),
                snapshot(122, boards=boards, workers=(actual,)),
            ])
            commands = FakeCommands()
            commands.predictions = {
                "build": PredictedPick("build", "t_b", "builder", "Build"),
                "review": PredictedPick("review", "t_r", "reviewquality", "Review"),
            }
            commands.outcome = CommandOutcome(False, ("t_other",), (("t_other", "reviewquality"),))
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

    def test_finishing_transition_at_final_fence_is_precommand_race(self) -> None:
        caps = AdmissionCaps(4, 2, (), 4, ())
        board = BoardView("a", {"t_new": "Build"}, {"t_new": "builder"}, None)
        active = LiveWorker("a", "t_old", 1, 41, 1.0, "reviewer", "running", "epoch|4100")
        finishing = LiveWorker("a", "t_old", 1, 41, 1.0, "reviewer", "done", "epoch|4100")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, fingerprint="active", boards=(board,), workers=(active,), caps=caps),
                snapshot(121, fingerprint="finishing", boards=(board,), workers=(finishing,), caps=caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_new", "builder", "Build")}
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "precommand-race")
            self.assertEqual(commands.calls, [])

    def test_multirow_dry_run_is_precommand_race_without_mutating_dispatch(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        with tempfile.TemporaryDirectory() as root:
            commands = FakeCommands()
            commands.predictions = {"a": PrecommandRace("more than one row")}
            engine = self.make_engine(root, FakeWorld([snapshot(120, boards=(board,))]), commands)
            self.assertEqual(engine.tick().reason, "precommand-race")
            self.assertEqual(commands.calls, [])

    def test_aging_never_bypasses_estop_manual_hold_or_host_cap(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        workers = (
            LiveWorker("x", "t_x", 1, 10, 1.0, "reviewquality", "running"),
            LiveWorker("y", "t_y", 2, 11, 2.0, "default", "running"),
        )
        cases = [
            (snapshot(120, estop=b"engaged", boards=(board,)), "estop"),
            (snapshot(120, manual_hold=True, boards=(board,)), "manual-hold"),
            (snapshot(120, boards=(board,), workers=workers), "host-cap"),
        ]
        for state, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                commands = FakeCommands()
                engine = self.make_engine(root, FakeWorld([state]), commands)
                engine.passed = {"a": 6}
                self.assertEqual(engine.tick().reason, reason)
                self.assertEqual(engine.passed, {"a": 6})
                self.assertEqual(commands.calls, [])

    def test_unowned_subscription_and_each_capacity_limit_hold_before_dispatch(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        profile_worker = LiveWorker("other", "t_x", 1, 10, 1.0, "builder", "running")
        board_worker = LiveWorker("a", "t_y", 2, 11, 2.0, "reviewquality", "running")
        host_workers = (
            profile_worker,
            LiveWorker("other-2", "t_z", 3, 12, 3.0, "default", "running"),
        )
        cases = [
            (snapshot(120, boards=(board,), unowned_subscriptions=1), "unowned-subscriptions"),
            (snapshot(120, boards=(board,), workers=(profile_worker,)), "profile-cap"),
            (snapshot(120, boards=(board,), workers=(board_worker,)), "board-cap"),
            (snapshot(120, boards=(board,), workers=host_workers), "host-cap"),
        ]
        for state, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                commands = FakeCommands()
                commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
                engine = self.make_engine(root, FakeWorld([state]), commands)
                self.assertEqual(engine.tick().reason, reason)
                self.assertEqual(commands.calls, [])

    def test_final_fence_cap_race_aborts_without_counting_window(self) -> None:
        boards = (
            BoardView("build", {"t_b": "Build"}, {"t_b": "builder"}, None),
            BoardView("review", {"t_r": "Review"}, {"t_r": "reviewquality"}, None),
        )
        final_worker = LiveWorker("other", "t_x", 1, 10, 1.0, "reviewquality", "running")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=boards),
                snapshot(121, boards=boards, workers=(final_worker,)),
            ])
            commands = FakeCommands()
            commands.predictions = {
                "build": PredictedPick("build", "t_b", "builder", "Build"),
                "review": PredictedPick("review", "t_r", "reviewquality", "Review"),
            }
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "profile-cap")
            self.assertEqual(engine.passed, {}, "an aborted admission is not a passed-over healthy window")
            self.assertEqual(commands.calls, [])

    def test_completed_command_consumes_the_full_recovery_window(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        actual = LiveWorker("a", "t_1", 7, 42, 1.0, "builder", "running")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(board,)),
                snapshot(121, boards=(board,)),
                snapshot(122, boards=(board,), workers=(actual,)),
                snapshot(150, boards=(board,), workers=(actual,)),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            commands.outcome = CommandOutcome(False, ("t_1",), (("t_1", "builder"),))
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "dispatched")
            self.assertEqual(engine.tick().reason, "recovery-dwell")
            self.assertEqual(commands.calls, [("dispatch", "a", "t_1")])

    def test_completion_race_reconciles_all_identities_and_dual_writes_receipt(self) -> None:
        caps = AdmissionCaps(4, 1, (("builder", 2),), 1, (("a", 4),))
        board = BoardView(
            "a",
            {"t_1": "Build one", "t_2": "Build two"},
            {"t_1": "builder", "t_2": "builder"},
            None,
            1,
        )
        first = LiveWorker("a", "t_1", 11, 101, 1.0, "builder", "running")
        second = LiveWorker("a", "t_2", 12, 102, 2.0, "builder", "running")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(board,), caps=caps),
                snapshot(121, boards=(board,), caps=caps),
                snapshot(122, boards=(board,), workers=(first, second), caps=caps),
                snapshot(150, boards=(board,), workers=(first, second), caps=caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build one")}
            commands.outcome = CommandOutcome(
                False,
                ("t_1", "t_2"),
                (("t_1", "builder"), ("t_2", "builder")),
            )
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "dispatched")
            self.assertEqual(commands.calls, [("dispatch", "a", "t_1")])
            self.assertEqual(commands.dispatch_maxes, [2, 2])
            journal = engine.store.read_json("pending.json")
            self.assertEqual(journal["actual_task_id"], "t_1")
            self.assertEqual(journal["actual_task_ids"], ["t_1", "t_2"])
            self.assertEqual(journal["extra_starts"], 1)
            status = engine.store.read_json("status.json")
            self.assertEqual(status["actual_task_ids"], ["t_1", "t_2"])
            self.assertEqual(status["extra_starts"], 1)
            self.assertEqual(engine.tick().reason, "recovery-dwell")
            self.assertEqual(commands.calls, [("dispatch", "a", "t_1")])

    def test_real_dispatch_max_uses_only_the_selected_board_database_count(self) -> None:
        caps = AdmissionCaps(4, 2, (), 1, (("a", 4),))
        selected = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None, 1)
        other = BoardView("b", {}, {}, None, 2)
        started = LiveWorker("a", "t_1", 11, 101, 1.0, "builder", "running")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(selected, other), caps=caps),
                snapshot(121, boards=(selected, other), caps=caps),
                snapshot(122, boards=(selected, other), workers=(started,), caps=caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            commands.outcome = CommandOutcome(False, ("t_1",), (("t_1", "builder"),))
            result = self.make_engine(root, world, commands).tick()
            self.assertEqual(result.reason, "dispatched")
            self.assertEqual(commands.calls, [("dispatch", "a", "t_1")])
            self.assertEqual(commands.dispatch_maxes, [2, 2])

    def test_final_fence_database_board_cap_issues_no_command(self) -> None:
        # Live workers leave admission capacity, but Hermes's running rows fill
        # the board: a hold, never an encoded --max 0 command.
        caps = AdmissionCaps(2, 1, (), 1, ())
        open_board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None, 0)
        full_board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None, 1)
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(open_board,), caps=caps),
                snapshot(121, boards=(full_board,), caps=caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "board-cap")
            self.assertEqual(commands.calls, [])
            self.assertEqual(commands.dispatch_maxes, [1])
            self.assertFalse((Path(root) / "state" / "pending.json").exists())

    def test_stricter_profile_and_host_caps_are_verified_after_dispatch(self) -> None:
        profile_caps = AdmissionCaps(4, 1, (("builder", 2),), 4, ())
        board = BoardView(
            "a",
            {"t_1": "Review one", "t_2": "Review two"},
            {"t_1": "reviewer", "t_2": "reviewer"},
            None,
            1,
        )
        starts = (
            LiveWorker("a", "t_1", 11, 101, 1.0, "reviewer", "running"),
            LiveWorker("a", "t_2", 12, 102, 2.0, "reviewer", "running"),
        )
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(board,), caps=profile_caps),
                snapshot(121, boards=(board,), caps=profile_caps),
                snapshot(122, boards=(board,), workers=starts, caps=profile_caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "reviewer", "Review one")}
            commands.outcome = CommandOutcome(False, ("t_1", "t_2"), (("t_1", "reviewer"), ("t_2", "reviewer")))
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "uncertain-outcome")
            self.assertEqual(engine.store.read_json("status.json")["cap_violation"]["name"], "reviewer")
            self.assertTrue(engine.store.has_pending_uncertainty())

        host_caps = AdmissionCaps(4, 4, (), 4, ())
        baseline = tuple(
            LiveWorker(f"b{i}", f"t_old{i}", i, i, float(i), f"p{i}", "done")
            for i in range(3)
        )
        post = baseline + starts
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(board,), workers=baseline, caps=host_caps),
                snapshot(121, boards=(board,), workers=baseline, caps=host_caps),
                snapshot(122, boards=(board,), workers=post, caps=host_caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "reviewer", "Review one")}
            commands.outcome = CommandOutcome(False, ("t_1", "t_2"), (("t_1", "reviewer"), ("t_2", "reviewer")))
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "uncertain-outcome")
            violation = engine.store.read_json("status.json")["cap_violation"]
            self.assertEqual((violation["reason"], violation["count"], violation["cap"]), ("host-cap", 5, 4))

    def test_post_difference_pairs_task_run_and_worker_identity(self) -> None:
        caps = AdmissionCaps(4, 2, (), 4, ())
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        before = LiveWorker("other", "t_manual", 1, 90, 9.0, "reviewer", "running", "epoch|90")
        changed_run = LiveWorker("other", "t_manual", 2, 90, 9.0, "reviewer", "running", "epoch|90")
        with tempfile.TemporaryDirectory() as root:
            world = FakeWorld([
                snapshot(120, boards=(board,), workers=(before,), caps=caps),
                snapshot(121, boards=(board,), workers=(before,), caps=caps),
                snapshot(122, boards=(board,), workers=(changed_run,), caps=caps),
            ])
            commands = FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            commands.outcome = CommandOutcome(False)
            engine = self.make_engine(root, world, commands)
            self.assertEqual(engine.tick().reason, "uncertain-outcome")
            self.assertTrue(engine.store.has_pending_uncertainty())

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
            commands.outcome = CommandOutcome(
                True, error=ErrorDetail("CommandTimeout", "timed\n out"),
            )
            first = self.make_engine(root, FakeWorld(states), commands).tick()
            self.assertEqual(first.reason, "uncertain-outcome")
            pending = self.make_engine(root, FakeWorld(states), commands).store.read_json("pending.json")
            self.assertEqual(pending["error"], {"type": "CommandTimeout", "message": "timed out"})

            restarted_commands = FakeCommands()
            restarted_engine = self.make_engine(root, FakeWorld([snapshot(120, boards=(board,))]), restarted_commands)
            restarted = restarted_engine.tick()
            self.assertEqual(restarted.reason, "uncertain-outcome")
            status = restarted_engine.store.read_json("status.json")
            self.assertEqual((status["error_type"], status["error"]), ("CommandTimeout", "timed out"))
            self.assertEqual(restarted_commands.calls, [])

    def test_each_engine_uncertainty_path_persists_bounded_reason(self) -> None:
        board = BoardView("a", {"t_1": "Build"}, {"t_1": "builder"}, None)
        started = LiveWorker("a", "t_1", 7, 42, 1.0, "builder", "running")

        def prepared(root: str, states, commands=None):
            commands = commands or FakeCommands()
            commands.predictions = {"a": PredictedPick("a", "t_1", "builder", "Build")}
            return self.make_engine(root, FakeWorld(states), commands), commands

        cases = []
        raising = FakeCommands()
        raising.dispatch = lambda *_args: (_ for _ in ()).throw(RuntimeError("boom\n" + "x" * 500))
        cases.append((
            [snapshot(120, boards=(board,)), snapshot(121, boards=(board,))], raising, "RuntimeError",
        ))
        uncertain = FakeCommands()
        uncertain.outcome = CommandOutcome(True, error=ErrorDetail("ContractDrift", "bad contract"))
        cases.append((
            [snapshot(120, boards=(board,)), snapshot(121, boards=(board,))], uncertain, "ContractDrift",
        ))
        post_capture = FakeCommands()
        post_capture.outcome = CommandOutcome(False, ("t_1",), (("t_1", "builder"),))
        cases.append((
            [snapshot(120, boards=(board,)), snapshot(121, boards=(board,)), RuntimeError("post failed")],
            post_capture, "RuntimeError",
        ))
        reconcile = FakeCommands()
        reconcile.outcome = CommandOutcome(False, ("t_1",), (("t_1", "builder"),))
        cases.append((
            [snapshot(120, boards=(board,)), snapshot(121, boards=(board,)), snapshot(122, boards=(board,))],
            reconcile, "RuntimeError",
        ))
        caps = AdmissionCaps(1, 1, (), 1, ())
        violation = FakeCommands()
        violation.outcome = CommandOutcome(False, ("t_1",), (("t_1", "builder"),))
        cases.append((
            [snapshot(120, boards=(board,), caps=caps), snapshot(121, boards=(board,), caps=caps),
             snapshot(122, boards=(board,), workers=(started, started), caps=caps)],
            violation, "CapacityViolation",
        ))

        for states, commands, expected_type in cases:
            with self.subTest(expected_type=expected_type), tempfile.TemporaryDirectory() as root:
                engine, commands = prepared(root, states, commands)
                self.assertEqual(engine.tick().reason, "uncertain-outcome")
                pending = engine.store.read_json("pending.json")
                status = engine.store.read_json("status.json")
                self.assertEqual(pending["error"]["type"], expected_type)
                self.assertEqual(status["error_type"], expected_type)
                self.assertEqual(status["error"], pending["error"]["message"])
                self.assertNotIn("\n", status["error"])
                self.assertLessEqual(len(status["error"]), 300)

    def test_legacy_pending_gets_safe_fallback_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            engine = self.make_engine(root, FakeWorld([]), FakeCommands())
            legacy = {"outcome": "pending", "kind": "dispatch", "task_id": "t_x"}
            engine.store.write_json("pending.json", legacy)
            self.assertEqual(engine.tick().reason, "uncertain-outcome")
            self.assertEqual(engine.store.read_json("pending.json"), legacy)
            status = engine.store.read_json("status.json")
            self.assertEqual(status["error_type"], "PendingUncertainty")
            self.assertIn("operator reconciliation", status["error"])

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_post_dispatch_capture_tolerates_unrelated_just_ended_worker(self, _epoch) -> None:
        caps = AdmissionCaps(4, 2, (), 4, ())
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            db = base / "board.db"
            connection = sqlite3.connect(db)
            connection.executescript("""
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                  priority INTEGER, created_at INTEGER, worker_pid INTEGER,
                  worker_started_at TEXT, current_run_id INTEGER
                );
                CREATE TABLE task_runs (
                  id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
                  worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER
                );
                CREATE TABLE kanban_notify_subs (task_id TEXT, notifier_profile TEXT);
            """)
            connection.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                ("t_a", "Existing", "reviewer", "running", 0, 1, 41, "epoch|4100", 1),
            )
            connection.execute(
                "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
                (1, "t_a", "reviewer", "running", 41, "epoch|4100", None),
            )
            connection.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                ("t_b", "Build", "builder", "ready", 10, 2, None, None, None),
            )
            connection.commit()
            connection.close()

            def process(pid, task, run, profile, start):
                return ProcessSnapshot(
                    pid, start / 100, 1,
                    ("/python", "-m", "hermes_cli.main", "-p", profile, "--cli",
                     "--accept-hooks", "chat", "-q", f"work kanban task {task}", "-Q"),
                    {"HERMES_KANBAN_TASK": task, "HERMES_KANBAN_RUN_ID": str(run),
                     "HERMES_KANBAN_BOARD": "a", "HERMES_PROFILE": profile},
                    True, start,
                )

            processes = [process(41, "t_a", 1, "reviewer", 4100)]
            raw = {
                "dispatch_in_gateway": False, "max_in_progress": 4,
                "max_in_progress_per_profile": 2, "failure_limit": 2,
                "auto_decompose": False, "reconcile_orphans": True,
                "dispatch_stale_timeout_seconds": 0, "review_dispatch": True,
                "default_assignee": None, "dispatch_profiles": ["builder", "reviewer"],
            }
            sample_clock = iter((120.0, 121.0, 122.0, 123.0))
            world = RuntimeWorld(
                boards={"a": db}, admission_caps=caps,
                sampler=lambda: host(next(sample_clock)), config_reader=lambda: raw,
                process_reader=lambda: tuple(processes), estop_paths=(), manual_hold=lambda: False,
            )

            class Commands:
                def predict(self, board, failure_limit, dispatch_max):
                    return PredictedPick("a", "t_b", "builder", "Build")

                def dispatch(self, pick, failure_limit, dispatch_max):
                    connection = sqlite3.connect(db)
                    connection.execute(
                        "UPDATE tasks SET status='done', worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL WHERE id='t_a'"
                    )
                    connection.execute("UPDATE task_runs SET status='done', ended_at=200 WHERE id=1")
                    connection.execute(
                        "UPDATE tasks SET status='running', worker_pid=42, worker_started_at='epoch|4200', current_run_id=2 WHERE id='t_b'"
                    )
                    connection.execute(
                        "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
                        (2, "t_b", "builder", "running", 42, "epoch|4200", None),
                    )
                    connection.commit()
                    connection.close()
                    processes.append(process(42, "t_b", 2, "builder", 4200))
                    return CommandOutcome(False, ("t_b",), (("t_b", "builder"),))

                def decompose(self, board, task_id):
                    raise AssertionError("not used")

            policy = AdmissionPolicy(recovery_seconds=0, max_sample_gap=35)
            policy.observe(host(110))
            engine = ControllerEngine(policy, SecureStateStore(base / "state"), world, Commands())
            self.assertEqual(engine.tick().reason, "dispatched")
            post = world.capture()
            self.assertEqual({worker.task_id for worker in post.workers}, {"t_a", "t_b"})
            self.assertEqual(len(post.workers), 2)


class StatusDeltaTests(unittest.TestCase):
    def _engine(self, root: str, samples: list[HostSample]) -> ControllerEngine:
        states = [
            GateSnapshot("same", sample, config(), None, True, (), (), 0)
            for sample in samples
        ]
        return ControllerEngine(
            AdmissionPolicy(recovery_seconds=120, max_sample_gap=35),
            SecureStateStore(Path(root) / "state"),
            FakeWorld(states),
            FakeCommands(),
        )

    def _status(self, engine: ControllerEngine) -> dict:
        return engine.store.read_json("status.json")

    def test_no_swap_delta_across_telemetry_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            engine = self._engine(root, [
                HostSample(0, 1, 10, 6 * GIB, "normal", 1000, 1000),
                HostSample(30, 1, 10, 6 * GIB, "normal", 2000, 1500),
                HostSample(10_000, 1, 10, 6 * GIB, "normal", 999_999, 999_999),
            ])
            engine.tick()
            engine.tick()
            self.assertEqual(self._status(engine)["swap_out_delta_bytes"], 500)
            engine.invalidate()
            result = engine.tick()
            self.assertEqual(result.reason, "swap-baseline")
            status = self._status(engine)
            self.assertIsNone(status["swap_in_delta_bytes"])
            self.assertIsNone(status["swap_out_delta_bytes"])
            self.assertIsNone(status["interval_seconds"])

    def test_no_swap_delta_on_baseline_gap_or_reset(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            engine = self._engine(root, [
                HostSample(0, 1, 10, 6 * GIB, "normal", 1000, 1000),
                HostSample(100, 1, 10, 6 * GIB, "normal", 5000, 5000),
                HostSample(130, 1, 10, 6 * GIB, "normal", 10, 10),
                HostSample(160, 1, 10, 6 * GIB, "normal", 20, 30),
            ])
            for expected in ("swap-baseline", "sample-gap", "swap-reset"):
                self.assertEqual(engine.tick().reason, expected)
                status = self._status(engine)
                self.assertIsNone(status["swap_in_delta_bytes"], expected)
                self.assertIsNone(status["swap_out_delta_bytes"], expected)
            engine.tick()
            status = self._status(engine)
            self.assertEqual(status["swap_in_delta_bytes"], 10)
            self.assertEqual(status["swap_out_delta_bytes"], 20)


if __name__ == "__main__":
    unittest.main()
