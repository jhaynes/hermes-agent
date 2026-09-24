from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from resource_controller.engine import ControllerEngine, GateSnapshot
from resource_controller.main import build_parser, run_iteration, set_manual_hold
from resource_controller.policy import AdmissionPolicy, HostSample
from resource_controller.storage import SecureStateStore
from resource_controller.telemetry import TelemetryError
from resource_controller.telemetry import constants

from test_engine import FakeCommands, config

GIB = 1024**3


class _ScriptedWorld:
    """Yields samples or raises TelemetryError per scripted step."""

    def __init__(self, steps: list) -> None:
        self.steps = steps
        self.index = 0

    def capture(self) -> GateSnapshot:
        step = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        if isinstance(step, Exception):
            raise step
        return GateSnapshot("same", step, config(), None, False, (), (), 0)


def _sample(now: float, swap: int = 100) -> HostSample:
    return HostSample(now, 1, 10, 6 * GIB, "normal", swap, swap)


class RunLoopTelemetryTests(unittest.TestCase):
    def _engine(self, root: str, world: _ScriptedWorld) -> ControllerEngine:
        return ControllerEngine(
            AdmissionPolicy(recovery_seconds=120, max_sample_gap=35),
            SecureStateStore(Path(root) / "state"),
            world,
            FakeCommands(),
        )

    def test_telemetry_error_invalidates_and_writes_bounded_status(self) -> None:
        err = TelemetryError(constants.ERROR_PARSE_ERROR, "bad\x1b[31m line\n" + "x" * 500)
        steps = [_sample(t) for t in (0, 30, 60, 90)] + [err] * 20 + [
            _sample(t) for t in (120, 150, 180, 210, 240)
        ]
        with tempfile.TemporaryDirectory() as root:
            world = _ScriptedWorld(steps)
            engine = self._engine(root, world)
            store = engine.store
            reasons = []
            for _ in range(4):
                run_iteration(engine, world, store, stopping=False)
                reasons.append(store.read_json("status.json")["reason"])
            self.assertEqual(reasons[-1], "recovery-dwell")
            for _ in range(20):
                self.assertIsNone(run_iteration(engine, world, store, stopping=False))
                status = store.read_json("status.json")
                self.assertEqual(status["reason"], "telemetry-error")
                self.assertEqual(status["error_code"], "parse-error")
                self.assertEqual(status["schema_version"], 2)
                self.assertIs(status["manual_hold"], False)
                self.assertLessEqual(len(status["diagnostic"]), 300)
                self.assertNotIn("\x1b", status["diagnostic"])
                self.assertNotIn("\n", status["diagnostic"])
                for stale in ("swap_in_bytes", "swap_out_bytes", "pressure", "pressure_detail",
                              "swap_in_delta_bytes", "swap_out_delta_bytes", "source"):
                    self.assertNotIn(stale, status)
            state_files = sorted(p.name for p in (Path(root) / "state").iterdir())
            self.assertEqual(state_files, ["status.json"])
            after = []
            for _ in range(5):
                run_iteration(engine, world, store, stopping=False)
                after.append(store.read_json("status.json")["reason"])
            self.assertEqual(after[0], "swap-baseline")
            self.assertEqual(after[1:4], ["recovery-dwell"] * 3)
            self.assertNotEqual(after[4], "recovery-dwell")
            self.assertEqual(after[4], "no-boards")

    def test_telemetry_error_reports_operator_hold_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            world = _ScriptedWorld([TelemetryError(constants.ERROR_PSI_UNAVAILABLE, "gone")])
            engine = self._engine(root, world)
            set_manual_hold(engine.store, True, "operator")
            run_iteration(engine, world, engine.store, stopping=False)
            status = engine.store.read_json("status.json")
            self.assertEqual(status["error_code"], "psi-unavailable")
            self.assertIs(status["manual_hold"], True)

    def test_other_exceptions_keep_persistent_operator_hold(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            world = _ScriptedWorld([RuntimeError("boom")])
            engine = self._engine(root, world)
            run_iteration(engine, world, engine.store, stopping=False)
            self.assertEqual(engine.store.read_json("status.json")["reason"], "persistent-operator-hold")


class MainSurfaceTests(unittest.TestCase):
    def test_cli_surface_separates_observation_and_activation(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["status", "--config", "/tmp/c"]).action, "status")
        self.assertEqual(parser.parse_args(["check", "--config", "/tmp/c"]).action, "check")
        self.assertEqual(parser.parse_args(["run", "--config", "/tmp/c"]).action, "run")

    def test_manual_hold_is_controller_owned_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            set_manual_hold(store, True, "operator")
            self.assertEqual(store.read_json("manual-hold.json")["engaged"], True)
            set_manual_hold(store, False, "operator")
            self.assertEqual(store.read_json("manual-hold.json")["engaged"], False)
            self.assertFalse((Path(root) / "ESTOP").exists())


if __name__ == "__main__":
    unittest.main()
