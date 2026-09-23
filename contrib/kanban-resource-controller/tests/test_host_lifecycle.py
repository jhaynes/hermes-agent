from __future__ import annotations

import tempfile
import unittest

from resource_controller.host import HostSampler, TelemetryError
from resource_controller.lifecycle import LifecycleGuard, LifecycleHold
from resource_controller.policy import AdmissionPolicy

GIB = 1024**3


class HostSamplerTests(unittest.TestCase):
    def test_native_values_are_mapped_without_swap_occupancy_policy(self) -> None:
        sampler = HostSampler(
            monotonic=lambda: 12.5,
            loadavg=lambda: (3.0, 2.0, 1.0),
            logical_cores=lambda: 10,
            available_memory=lambda: 6 * GIB,
            paging_counters=lambda: (123, 456),
            pressure_level=lambda: 1,
        )
        result = sampler.sample()
        self.assertEqual(result.pressure, "normal")
        self.assertEqual((result.page_in, result.page_out), (123, 456))

    def test_unknown_pressure_or_sensor_failure_is_telemetry_error(self) -> None:
        for pressure in (0, 3, None):
            with self.subTest(pressure=pressure), self.assertRaises(TelemetryError):
                HostSampler(
                    monotonic=lambda: 1,
                    loadavg=lambda: (1, 1, 1),
                    logical_cores=lambda: 4,
                    available_memory=lambda: 6 * GIB,
                    paging_counters=lambda: (1, 1),
                    pressure_level=lambda: pressure,
                ).sample()

        def broken() -> tuple[float, float, float]:
            raise OSError("sensor unavailable")

        with self.assertRaises(TelemetryError):
            HostSampler(loadavg=broken).sample()


class LifecycleTests(unittest.TestCase):
    def test_resume_only_clears_own_hold_and_resets_dwell(self) -> None:
        policy = AdmissionPolicy()
        guard = LifecycleGuard(policy)
        guard.hold("operator")
        self.assertTrue(guard.manual_hold)
        guard.resume(now=50)
        self.assertFalse(guard.manual_hold)
        self.assertIsNone(guard.estop_action)

    def test_stop_refuses_commands_workers_and_unknown_descendants(self) -> None:
        policy = AdmissionPolicy()
        guard = LifecycleGuard(policy)
        cases = [
            {"command_running": True, "workers": 0, "descendants_known": True},
            {"command_running": False, "workers": 1, "descendants_known": True},
            {"command_running": False, "workers": 0, "descendants_known": False},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(LifecycleHold):
                guard.request_stop(**kwargs)
        guard.request_stop(command_running=False, workers=0, descendants_known=True)
        self.assertTrue(guard.stop_requested)


if __name__ == "__main__":
    unittest.main()
