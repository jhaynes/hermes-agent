from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from resource_controller.config import ConfigCompatibilityError
from resource_controller.engine import ControllerEngine, GateSnapshot
from resource_controller.main import _check, build_parser, run_iteration, set_manual_hold
from resource_controller.inventory import LiveWorker
from resource_controller.policy import AdmissionPolicy, HostSample
from resource_controller.storage import SecureStateStore
from resource_controller.telemetry import TelemetryError
from resource_controller.telemetry import constants
from resource_controller.spec import AdmissionCaps

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


class CheckOutputTests(unittest.TestCase):
    def test_check_reports_effective_caps_expected_observed_counts_and_offender(self) -> None:
        caps = AdmissionCaps(4, 1, (), 1, (("smithers", 4),))
        workers = (
            LiveWorker("smithers", "t_1", 1, 10, 1.0, "builder", "running"),
            LiveWorker("smithers", "t_2", 2, 11, 2.0, "builder", "running"),
        )
        board = SimpleNamespace(board="smithers", hermes_db_running_count=1)
        state = GateSnapshot(
            "fingerprint", _sample(1), config(caps=caps), None, False,
            workers, (board,), 0,
        )
        spec = SimpleNamespace(admission_caps=caps, hermes_home=Path("/unused"))
        output = io.StringIO()
        with mock.patch("resource_controller.main._world", return_value=SimpleNamespace(capture=lambda: state)), redirect_stdout(output):
            self.assertEqual(_check(spec, mock.Mock()), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["admission"]["host_cap"], 4)
        self.assertEqual(payload["admission"]["expected_hermes"], payload["admission"]["observed_hermes"])
        self.assertEqual(payload["boards"]["smithers"]["hermes_db_running_count"], 1)
        self.assertEqual(payload["boards"]["smithers"]["controller_reconciled_live_count"], 2)
        self.assertEqual(payload["admission"]["cap_violation"]["name"], "builder")

    def test_check_makes_hermes_cap_mismatch_obvious(self) -> None:
        caps = AdmissionCaps(4, 1, (), 1, ())
        spec = SimpleNamespace(admission_caps=caps, hermes_home=Path("/unused"))
        failing = SimpleNamespace(capture=mock.Mock(side_effect=ConfigCompatibilityError("max_in_progress must be exactly 4")))
        output = io.StringIO()
        with (
            mock.patch("resource_controller.main._world", return_value=failing),
            mock.patch("resource_controller.main.read_kanban_config", return_value={
                "max_in_progress": 2, "max_in_progress_per_profile": 1,
            }),
            redirect_stdout(output),
        ):
            self.assertEqual(_check(spec, mock.Mock()), 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["compatible"])
        self.assertEqual(payload["expected_hermes"]["kanban.max_in_progress"], 4)
        self.assertEqual(payload["observed_hermes"]["kanban.max_in_progress"], 2)


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
