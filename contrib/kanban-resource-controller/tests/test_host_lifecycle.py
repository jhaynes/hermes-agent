from __future__ import annotations

import unittest

from resource_controller.host import HostSampler, TelemetryError
from resource_controller.spec import DEFAULT_MAX_SAMPLE_GAP_SECONDS, DEFAULT_RECOVERY_SECONDS
from resource_controller.lifecycle import LifecycleGuard, LifecycleHold
from resource_controller.policy import AdmissionPolicy
from resource_controller.telemetry import MemoryHealth

GIB = 1024**3


class _FakeBackend:
    def __init__(self, health=None, error=None):
        self._health = health
        self._error = error

    def sample(self):
        if self._error is not None:
            raise self._error
        return self._health


def _health(**overrides) -> MemoryHealth:
    base = dict(
        available_bytes=6 * GIB,
        swap_in_bytes=123,
        swap_out_bytes=456,
        counter_page_size_bytes=16384,
        pressure="normal",
        pressure_detail={"level": 1},
        source="darwin-vm_stat+sysctl",
    )
    base.update(overrides)
    return MemoryHealth(**base)


class HostSamplerTests(unittest.TestCase):
    def test_native_values_are_mapped_without_swap_occupancy_policy(self) -> None:
        sampler = HostSampler(
            monotonic=lambda: 12.5,
            loadavg=lambda: (3.0, 2.0, 1.0),
            logical_cores=lambda: 10,
            backend=_FakeBackend(health=_health()),
        )
        result = sampler.sample()
        self.assertEqual(result.pressure, "normal")
        self.assertEqual((result.swap_in, result.swap_out), (123, 456))
        self.assertEqual(result.counter_page_size_bytes, 16384)
        self.assertEqual(result.source, "darwin-vm_stat+sysctl")

    def test_backend_telemetry_error_propagates(self) -> None:
        with self.assertRaises(TelemetryError):
            HostSampler(
                monotonic=lambda: 1,
                loadavg=lambda: (1, 1, 1),
                logical_cores=lambda: 4,
                backend=_FakeBackend(error=TelemetryError("parse-error", "boom")),
            ).sample()

    def test_sensor_failure_is_telemetry_error(self) -> None:
        def broken() -> tuple[float, float, float]:
            raise OSError("sensor unavailable")

        with self.assertRaises(TelemetryError):
            HostSampler(loadavg=broken, backend=_FakeBackend(health=_health())).sample()


class LifecycleTests(unittest.TestCase):
    def test_resume_only_clears_own_hold_and_resets_dwell(self) -> None:
        policy = AdmissionPolicy(recovery_seconds=DEFAULT_RECOVERY_SECONDS, max_sample_gap=DEFAULT_MAX_SAMPLE_GAP_SECONDS)
        guard = LifecycleGuard(policy)
        guard.hold("operator")
        self.assertTrue(guard.manual_hold)
        guard.resume(now=50)
        self.assertFalse(guard.manual_hold)
        self.assertIsNone(guard.estop_action)

    def test_stop_refuses_commands_workers_and_unknown_descendants(self) -> None:
        policy = AdmissionPolicy(recovery_seconds=DEFAULT_RECOVERY_SECONDS, max_sample_gap=DEFAULT_MAX_SAMPLE_GAP_SECONDS)
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
