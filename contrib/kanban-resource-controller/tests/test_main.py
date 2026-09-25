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
from resource_controller.main import _check, admission_policy, build_parser, log_transition, run_iteration, set_manual_hold
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
        spec = SimpleNamespace(admission_caps=caps, hermes_home=Path("/unused"), interval_seconds=10, recovery_seconds=5.0, max_sample_gap_seconds=25.0)
        output = io.StringIO()
        with mock.patch("resource_controller.main._world", return_value=SimpleNamespace(capture=lambda: state)), redirect_stdout(output):
            self.assertEqual(_check(spec, mock.Mock()), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["admission"]["host_cap"], 4)
        self.assertEqual(payload["admission"]["expected_hermes"], payload["admission"]["observed_hermes"])
        self.assertEqual(payload["boards"]["smithers"]["hermes_db_running_count"], 1)
        self.assertEqual(payload["boards"]["smithers"]["controller_reconciled_live_count"], 2)
        self.assertEqual(payload["admission"]["cap_violation"]["name"], "builder")
        self.assertEqual(payload["pacing"], {
            "interval_seconds": 10, "recovery_seconds": 5.0, "max_sample_gap_seconds": 25.0,
        })

    def test_check_makes_hermes_cap_mismatch_obvious(self) -> None:
        caps = AdmissionCaps(4, 1, (), 1, ())
        spec = SimpleNamespace(admission_caps=caps, hermes_home=Path("/unused"), interval_seconds=10, recovery_seconds=5.0, max_sample_gap_seconds=25.0)
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


class PacingWiringTests(unittest.TestCase):
    def test_runtime_policy_takes_pacing_from_the_spec(self) -> None:
        spec = SimpleNamespace(recovery_seconds=0.0, max_sample_gap_seconds=25.0)
        policy = admission_policy(spec)
        self.assertEqual((policy.recovery_seconds, policy.max_sample_gap), (0.0, 25.0))
        self.assertEqual(policy.observe(_sample(0)).reason, "swap-baseline")
        self.assertTrue(policy.observe(_sample(10)).eligible, "zero recovery admits on the next healthy sample")
        policy.command_consumed(10)
        self.assertTrue(policy.observe(_sample(20)).eligible, "a started command costs no dwell at recovery 0")
        self.assertEqual(policy.observe(_sample(50)).reason, "sample-gap", "30 s > configured 25 s gap")

    def test_short_recovery_needs_one_clean_sample_after_a_bad_one(self) -> None:
        policy = admission_policy(SimpleNamespace(recovery_seconds=5.0, max_sample_gap_seconds=25.0))
        policy.observe(_sample(0))
        self.assertTrue(policy.observe(_sample(10)).eligible)
        warning = HostSample(20, 1, 10, 6 * GIB, "warning", 100, 100)
        self.assertEqual(policy.observe(warning).reason, "pressure")
        self.assertEqual(policy.observe(_sample(30)).reason, "recovery-dwell")
        self.assertTrue(policy.observe(_sample(40)).eligible)


    def test_sample_gap_boundary_is_inclusive_of_the_configured_gap(self) -> None:
        policy = admission_policy(SimpleNamespace(recovery_seconds=0.0, max_sample_gap_seconds=25.0))
        policy.observe(_sample(0))
        self.assertTrue(policy.observe(_sample(25)).eligible, "a gap exactly equal to the limit is continuous")
        self.assertEqual(policy.observe(_sample(50.5)).reason, "sample-gap", "25.5 s exceeds the 25 s limit")

    def test_command_consumed_sets_the_exact_dwell_baseline(self) -> None:
        policy = admission_policy(SimpleNamespace(recovery_seconds=5.0, max_sample_gap_seconds=25.0))
        policy.observe(_sample(0))
        self.assertTrue(policy.observe(_sample(10)).eligible)
        policy.command_consumed(10)
        almost = policy.observe(_sample(14.999))
        self.assertEqual(almost.reason, "recovery-dwell")
        self.assertAlmostEqual(almost.healthy_seconds, 4.999, places=6)
        exact = policy.observe(_sample(15))
        self.assertTrue(exact.eligible, "eligible exactly recovery_seconds after the consumed instant")
        self.assertEqual(exact.healthy_seconds, 5.0)


class TransitionLogTests(unittest.TestCase):
    def test_logs_starts_and_reason_changes_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            out = io.StringIO()
            last = None
            sequence = [
                {"reason": "recovery-dwell", "actual_task_ids": []},
                {"reason": "recovery-dwell", "actual_task_ids": []},
                {"reason": "dispatched", "actual_task_ids": ["t_a", "t_b"], "extra_starts": 1},
                {"reason": "pressure", "actual_task_ids": [], "observed_blockers": ["pressure"], "pressure": "warning"},
                {"reason": "pressure", "actual_task_ids": [], "observed_blockers": ["pressure"], "pressure": "warning"},
                {"reason": "eligible", "actual_task_ids": []},
            ]
            for status in sequence:
                store.write_json("status.json", status)
                last = log_transition(store, last, stream=out)
            lines = [line.split(" ", 1)[1] for line in out.getvalue().splitlines()]
            self.assertEqual(lines, [
                "reason=recovery-dwell",
                "reason=dispatched started=t_a,t_b extra_starts=1",
                "reason=pressure blockers=pressure pressure=warning",
                "reason=eligible",
            ])

    def test_unreadable_status_is_logged_not_raised(self) -> None:
        store = mock.Mock()
        store.read_json.side_effect = OSError("gone")
        out = io.StringIO()
        self.assertEqual(log_transition(store, ("x", (), ()), stream=out), ("x", (), ()))
        self.assertIn("status-unreadable OSError", out.getvalue())


class RunLoopWiringTests(unittest.TestCase):
    def test_run_loop_uses_spec_pacing_and_logs_start_transitions_and_stop(self) -> None:
        from resource_controller import main as main_module

        caps = AdmissionCaps(8, 1, (), 2, ())
        spec = SimpleNamespace(
            interval_seconds=7, recovery_seconds=3.0, max_sample_gap_seconds=20.0,
            admission_caps=caps, dispatcher_lock=Path("/unused/lock"),
        )
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            statuses = iter([
                ({"reason": "recovery-dwell", "actual_task_ids": []}, None),
                ({"reason": "dispatched", "actual_task_ids": ["t_x"]}, None),
                ({"reason": "recovery-dwell", "actual_task_ids": []}, 0),
            ])
            seen = {}

            def fake_iteration(engine, world, store_, *, stopping):
                seen["policy"] = engine.policy
                status, code = next(statuses)
                store_.write_json("status.json", status)
                return code

            sleeps = []
            out = io.StringIO()
            with (
                mock.patch.object(main_module, "_world", return_value=mock.Mock()),
                mock.patch.object(main_module, "_commands", return_value=mock.Mock()),
                mock.patch.object(main_module, "SingletonLock", return_value=mock.MagicMock()),
                mock.patch.object(main_module, "run_iteration", side_effect=fake_iteration),
                mock.patch.object(main_module.signal, "signal"),
                mock.patch.object(main_module.time, "sleep", side_effect=sleeps.append),
                redirect_stdout(out),
            ):
                self.assertEqual(main_module._run(spec, store), 0)
        self.assertEqual(sleeps, [7, 7])
        self.assertEqual((seen["policy"].recovery_seconds, seen["policy"].max_sample_gap), (3.0, 20.0))
        lines = [line.split(" ", 1)[1] for line in out.getvalue().splitlines()]
        self.assertEqual(lines, [
            "started interval=7s recovery=3s max_sample_gap=20s host_cap=8",
            "reason=recovery-dwell",
            "reason=dispatched started=t_x",
            "reason=recovery-dwell",
            "stopped exit=0",
        ])


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
