from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from resource_controller.alerting import StuckAlertTracker
from resource_controller.storage import SecureStateStore


class StuckAlertTrackerTests(unittest.TestCase):
    def test_threshold_boundary_details_changes_and_restart_are_one_incident(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            calls: list[dict] = []
            tracker = StuckAlertTracker(store)
            tracker.observe(
                {"reason": "uncertain-outcome", "error_type": "IdentityHold", "error": "first"},
                now=100.0, threshold=10.0, notify=calls.append,
            )
            tracker.observe(
                {"reason": "persistent-operator-hold", "error_type": "RuntimeError", "error": "latest"},
                now=109.999, threshold=10.0, notify=calls.append,
            )
            self.assertEqual(calls, [])
            tracker.observe(
                {"reason": "persistent-operator-hold", "error_type": "RuntimeError", "error": "latest"},
                now=110.0, threshold=10.0, notify=calls.append,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["error"], "latest")
            StuckAlertTracker(store).observe(
                {"reason": "uncertain-outcome", "error_type": "Changed", "error": "after restart"},
                now=999.0, threshold=0.0, notify=calls.append,
            )
            self.assertEqual(len(calls), 1)

    def test_normal_status_resets_incident_and_nonstuck_reasons_never_alert(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            calls: list[dict] = []
            tracker = StuckAlertTracker(store)
            for reason in ("pressure", "manual-hold", "recovery-dwell"):
                tracker.observe({"reason": reason}, now=1.0, threshold=0.0, notify=calls.append)
            self.assertEqual(calls, [])
            tracker.observe({"reason": "uncertain-outcome", "error": "one"}, now=2.0, threshold=0.0, notify=calls.append)
            tracker.observe({"reason": "eligible"}, now=3.0, threshold=0.0, notify=calls.append)
            tracker.observe({"reason": "uncertain-outcome", "error": "two"}, now=4.0, threshold=0.0, notify=calls.append)
            self.assertEqual(len(calls), 2)

    def test_callback_failure_is_attempted_once_and_malformed_state_is_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            pending = {"outcome": "pending", "error": {"type": "IdentityHold", "message": "x"}}
            store.write_json("pending.json", pending)
            attempts = 0

            def failing(_details: dict) -> None:
                nonlocal attempts
                attempts += 1
                raise RuntimeError("notification failed after log fallback")

            tracker = StuckAlertTracker(store)
            tracker.observe({"reason": "uncertain-outcome", "error": "x"}, now=1.0, threshold=0.0, notify=failing)
            tracker.observe({"reason": "uncertain-outcome", "error": "x"}, now=2.0, threshold=0.0, notify=failing)
            self.assertEqual(attempts, 1)
            self.assertIsNotNone(store.read_json("alert-state.json")["alert_attempted_at"])
            self.assertEqual(store.read_json("pending.json"), pending)

            store.write_json("alert-state.json", {"broken": True})
            calls: list[dict] = []
            StuckAlertTracker(store).observe(
                {"reason": "persistent-operator-hold", "error": "new"},
                now=3.0, threshold=0.0, notify=calls.append,
            )
            self.assertEqual(len(calls), 1)

    def test_alert_details_are_sanitized_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            calls: list[dict] = []
            StuckAlertTracker(store).observe(
                {"reason": "uncertain-outcome", "error_type": "Bad\nType", "error": "x\t\x1b[31m" + "y" * 500},
                now=1.0, threshold=0.0, notify=calls.append,
            )
            self.assertEqual(len(calls), 1)
            self.assertNotIn("\n", calls[0]["error_type"])
            self.assertNotIn("\x1b", calls[0]["error"])
            self.assertLessEqual(len(calls[0]["error"]), 300)

    def test_backwards_clock_resets_future_incident_start(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SecureStateStore(Path(root) / "state")
            store.write_json("alert-state.json", {
                "active": True,
                "started_at": 200.0,
                "alert_attempted_at": None,
                "reason": "uncertain-outcome",
                "error_type": "IdentityHold",
                "error": "future",
            })
            calls: list[dict] = []
            tracker = StuckAlertTracker(store)
            tracker.observe(
                {"reason": "uncertain-outcome", "error_type": "IdentityHold", "error": "current"},
                now=100.0, threshold=10.0, notify=calls.append,
            )
            self.assertEqual(calls, [])
            self.assertEqual(store.read_json("alert-state.json")["started_at"], 100.0)
            tracker.observe(
                {"reason": "uncertain-outcome", "error_type": "IdentityHold", "error": "current"},
                now=110.0, threshold=10.0, notify=calls.append,
            )
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
