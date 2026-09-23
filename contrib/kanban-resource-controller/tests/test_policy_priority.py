from __future__ import annotations

import math
import unittest

from resource_controller.policy import AdmissionPolicy, HostSample
from resource_controller.priority import PredictedPick, Stage, classify_stage, select_pick

GIB = 1024**3


def sample(
    now: float,
    *,
    load1: float = 1.0,
    cores: int = 10,
    available: int = 6 * GIB,
    pressure: str = "normal",
    page_in: int = 100,
    page_out: int = 100,
) -> HostSample:
    return HostSample(now, load1, cores, available, pressure, page_in, page_out)


class AdmissionPolicyTests(unittest.TestCase):
    def test_first_sample_holds_then_full_quiet_dwell_recovers(self) -> None:
        policy = AdmissionPolicy(recovery_seconds=120, max_sample_gap=35)
        self.assertEqual(policy.observe(sample(0)).reason, "paging-baseline")
        self.assertEqual(policy.observe(sample(30)).reason, "recovery-dwell")
        self.assertEqual(policy.observe(sample(119)).reason, "sample-gap")

        policy = AdmissionPolicy(recovery_seconds=120, max_sample_gap=35)
        for now in (0, 30, 60, 90):
            self.assertFalse(policy.observe(sample(now)).eligible)
        self.assertTrue(policy.observe(sample(120)).eligible)

    def test_threshold_boundaries_fail_closed(self) -> None:
        cases = [
            (sample(30, load1=10), "load"),
            (sample(30, available=4 * GIB - 1), "memory"),
            (sample(30, pressure="warning"), "pressure"),
            (sample(30, pressure="critical"), "pressure"),
            (sample(30, page_in=101), "paging"),
            (sample(30, page_out=101), "paging"),
        ]
        for current, reason in cases:
            with self.subTest(reason=reason, current=current):
                policy = AdmissionPolicy()
                policy.observe(sample(0))
                self.assertEqual(policy.observe(current).reason, reason)

        policy = AdmissionPolicy(recovery_seconds=30, max_sample_gap=35)
        self.assertEqual(policy.observe(sample(0, available=4 * GIB)).reason, "paging-baseline")
        self.assertEqual(
            policy.observe(sample(30, available=4 * GIB)).reason,
            "recovery-memory",
            "the initial-memory boundary is allowed but cannot satisfy the 5 GiB recovery band",
        )
        self.assertEqual(policy.observe(sample(60, available=5 * GIB)).reason, "recovery-dwell")
        self.assertTrue(policy.observe(sample(90, available=5 * GIB)).eligible)

    def test_unknown_reset_and_nonmonotonic_samples_fail_closed(self) -> None:
        invalid = [
            sample(30, load1=math.nan),
            sample(30, cores=0),
            sample(30, available=-1),
            sample(30, pressure="unknown"),
        ]
        for current in invalid:
            with self.subTest(current=current):
                policy = AdmissionPolicy()
                policy.observe(sample(0))
                self.assertEqual(policy.observe(current).reason, "unknown-telemetry")

        policy = AdmissionPolicy()
        policy.observe(sample(0, page_in=100))
        self.assertEqual(policy.observe(sample(30, page_in=99)).reason, "paging-reset")
        self.assertEqual(policy.observe(sample(20, page_in=99)).reason, "nonmonotonic-time")

        policy = AdmissionPolicy(recovery_seconds=30, max_sample_gap=35)
        policy.observe(sample(0, page_out=100))
        self.assertEqual(policy.observe(sample(30, page_out=99)).reason, "paging-reset")
        self.assertEqual(policy.observe(sample(60, page_out=99)).reason, "recovery-dwell")
        self.assertTrue(policy.observe(sample(90, page_out=99)).eligible)

    def test_recovery_requires_strict_recovery_band_and_command_resets_it(self) -> None:
        policy = AdmissionPolicy(recovery_seconds=120, max_sample_gap=35)
        policy.observe(sample(0))
        self.assertEqual(policy.observe(sample(30, load1=8.1)).reason, "recovery-load")
        for now in (60, 90, 120, 150, 180):
            result = policy.observe(sample(now, load1=8.0))
        self.assertTrue(result.eligible)
        policy.command_consumed(180)
        self.assertEqual(policy.observe(sample(210)).reason, "recovery-dwell")


class PriorityTests(unittest.TestCase):
    def test_stage_classification_is_deterministic(self) -> None:
        self.assertEqual(classify_stage("builder", "Resolve merge conflict"), Stage.MERGE_CONFLICT)
        self.assertEqual(classify_stage("builder-special", "REBASE main"), Stage.MERGE_CONFLICT)
        self.assertEqual(classify_stage("reviewtests", "anything"), Stage.REVIEW)
        self.assertEqual(classify_stage("default", "merge docs"), Stage.BUILD)
        self.assertEqual(classify_stage(None, "review implementation"), Stage.BUILD)

    def test_highest_stage_wins_and_round_robin_breaks_ties(self) -> None:
        picks = [
            PredictedPick("a", "ta", "default", "build"),
            PredictedPick("b", "tb", "reviewquality", "review"),
            PredictedPick("c", "tc", "reviewtests", "review"),
        ]
        selected = select_pick(picks, board_order=["a", "b", "c"], pointer=2, passed={})
        self.assertEqual(selected.pick.board, "c")
        self.assertEqual(selected.next_pointer, 0)
        self.assertEqual(selected.passed, {"a": 1, "b": 1, "c": 0})

    def test_six_passed_windows_force_one_aging_admission(self) -> None:
        picks = [
            PredictedPick("build", "t1", "default", "compile"),
            PredictedPick("review", "t2", "reviewscope", "review"),
        ]
        selected = select_pick(
            picks,
            board_order=["build", "review"],
            pointer=0,
            passed={"build": 6, "review": 0},
        )
        self.assertEqual(selected.pick.board, "build")
        self.assertEqual(selected.passed["build"], 0)

    def test_invalid_prediction_is_not_a_pick(self) -> None:
        with self.assertRaises(ValueError):
            PredictedPick("board", "", "default", "title")


if __name__ == "__main__":
    unittest.main()
