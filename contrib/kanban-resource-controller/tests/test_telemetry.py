from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from resource_controller.host import HostSampler
from resource_controller.policy import AdmissionPolicy, HostSample
from resource_controller.telemetry import MemoryHealth, TelemetryError, select_backend
from resource_controller.telemetry import constants
from resource_controller.telemetry.darwin import DarwinBackend, parse_pressure_level, parse_vm_stat
from resource_controller.telemetry.linux import (
    LinuxBackend,
    parse_meminfo,
    parse_pressure_memory,
    parse_vmstat,
)

FIXTURES = Path(__file__).parent / "fixtures"
GIB = 1024**3


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fake_completed(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    return subprocess.CompletedProcess(args=(), returncode=returncode, stdout=stdout, stderr=stderr)


class DarwinParserTests(unittest.TestCase):
    def test_real_16k_fixture_parses(self) -> None:
        text = _fixture("vm_stat_darwin_16k.txt")
        page_size, swapins, swapouts = parse_vm_stat(text, b"", 0)
        self.assertEqual(page_size, 16384)
        self.assertEqual(swapins, 1775093)
        self.assertEqual(swapouts, 2961768)

    def test_4k_variant_fixture_parses(self) -> None:
        text = _fixture("vm_stat_darwin_4k.txt")
        page_size, _, _ = parse_vm_stat(text, b"", 0)
        self.assertEqual(page_size, 4096)

    def test_missing_swapins_line_raises(self) -> None:
        text = _fixture("vm_stat_darwin_16k.txt").decode().replace("Swapins:", "Xwapins:").encode()
        with self.assertRaises(TelemetryError):
            parse_vm_stat(text, b"", 0)

    def test_duplicate_swapouts_line_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        dup = base + "\nSwapouts:                                     2961768.\n"
        with self.assertRaises(TelemetryError):
            parse_vm_stat(dup.encode(), b"", 0)

    def test_missing_page_size_header_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        stripped = base.replace("(page size of 16384 bytes)", "")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(stripped.encode(), b"", 0)

    def test_zero_page_size_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        bad = base.replace("page size of 16384 bytes", "page size of 0 bytes")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(bad.encode(), b"", 0)

    def test_non_power_of_two_page_size_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        bad = base.replace("page size of 16384 bytes", "page size of 12000 bytes")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(bad.encode(), b"", 0)

    def test_out_of_range_page_size_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        too_big = base.replace("page size of 16384 bytes", "page size of 131072 bytes")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(too_big.encode(), b"", 0)
        too_small = base.replace("page size of 16384 bytes", "page size of 2048 bytes")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(too_small.encode(), b"", 0)

    def test_non_numeric_value_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        bad = base.replace("Swapins:                                     1775093.", "Swapins:                                     abc.")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(bad.encode(), b"", 0)

    def test_missing_trailing_period_raises(self) -> None:
        base = _fixture("vm_stat_darwin_16k.txt").decode()
        bad = base.replace("Swapins:                                     1775093.", "Swapins:                                     1775093")
        with self.assertRaises(TelemetryError):
            parse_vm_stat(bad.encode(), b"", 0)

    def test_oversized_output_raises(self) -> None:
        oversized = b"x" * (constants.DARWIN_MAX_OUTPUT_BYTES + 1)
        with self.assertRaises(TelemetryError):
            parse_vm_stat(oversized, b"", 0)

    def test_nonzero_exit_raises(self) -> None:
        with self.assertRaises(TelemetryError):
            parse_vm_stat(_fixture("vm_stat_darwin_16k.txt"), b"", 1)

    def test_undecodable_bytes_raise(self) -> None:
        with self.assertRaises(TelemetryError):
            parse_vm_stat(b"\xff\xfe\x00garbage", b"", 0)

    def test_timeout_raises_vm_stat_timeout_code(self) -> None:
        def timeout_runner(argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        backend = DarwinBackend(runner=timeout_runner, available_memory=lambda: 6 * GIB)
        with self.assertRaises(TelemetryError) as ctx:
            backend.sample()
        self.assertEqual(ctx.exception.error_code, constants.ERROR_VM_STAT_TIMEOUT)


class DarwinPressureTests(unittest.TestCase):
    def test_boundary_levels_map_to_enum(self) -> None:
        self.assertEqual(parse_pressure_level(b"1", 0), 1)
        self.assertEqual(parse_pressure_level(b"2", 0), 2)
        self.assertEqual(parse_pressure_level(b"4", 0), 4)

    def test_unknown_level_is_telemetry_error_at_backend(self) -> None:
        def runner(argv):
            if argv[0].endswith("vm_stat"):
                return _fake_completed(stdout=_fixture("vm_stat_darwin_16k.txt"))
            return _fake_completed(stdout=b"3")

        backend = DarwinBackend(runner=runner, available_memory=lambda: 6 * GIB)
        with self.assertRaises(TelemetryError):
            backend.sample()

    def test_psutil_swap_memory_never_called_on_darwin(self) -> None:
        import psutil

        calls = []
        original = psutil.swap_memory

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        psutil.swap_memory = spy
        try:
            def runner(argv):
                if argv[0].endswith("vm_stat"):
                    return _fake_completed(stdout=_fixture("vm_stat_darwin_16k.txt"))
                return _fake_completed(stdout=b"1")

            backend = DarwinBackend(runner=runner, available_memory=lambda: 6 * GIB)
            backend.sample()
            self.assertEqual(calls, [])
        finally:
            psutil.swap_memory = original


class DarwinBackendSampleTests(unittest.TestCase):
    def test_sample_end_to_end_with_fixture(self) -> None:
        def runner(argv):
            if argv[0].endswith("vm_stat"):
                return _fake_completed(stdout=_fixture("vm_stat_darwin_16k.txt"))
            return _fake_completed(stdout=b"1")

        backend = DarwinBackend(runner=runner, available_memory=lambda: 6 * GIB)
        health = backend.sample()
        self.assertEqual(health.source, constants.SOURCE_DARWIN)
        self.assertEqual(health.pressure, "normal")
        self.assertEqual(health.counter_page_size_bytes, 16384)
        self.assertEqual(health.swap_in_bytes, 1775093 * 16384)
        self.assertEqual(health.swap_out_bytes, 2961768 * 16384)
        self.assertEqual(health.pressure_detail, {"level": 1})


class LinuxParserTests(unittest.TestCase):
    def test_real_vmstat_fixture_parses(self) -> None:
        pswpin, pswpout = parse_vmstat(_fixture("proc_vmstat.txt").decode())
        self.assertEqual((pswpin, pswpout), (0, 0))

    def test_real_meminfo_fixture_parses(self) -> None:
        available = parse_meminfo(_fixture("proc_meminfo.txt").decode())
        self.assertEqual(available, 7644888 * 1024)

    def test_real_pressure_memory_fixture_parses(self) -> None:
        psi = parse_pressure_memory(_fixture("proc_pressure_memory.txt").decode())
        self.assertEqual(psi["some_avg10"], 0.0)
        self.assertEqual(psi["full_avg10"], 0.0)

    def test_missing_full_line_raises(self) -> None:
        text = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        with self.assertRaises(TelemetryError):
            parse_pressure_memory(text)

    def test_non_float_avg_raises(self) -> None:
        text = (
            "some avg10=abc avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        with self.assertRaises(TelemetryError):
            parse_pressure_memory(text)

    def test_duplicate_pswpin_raises(self) -> None:
        text = "pswpin 0\npswpin 1\npswpout 0\n"
        with self.assertRaises(TelemetryError):
            parse_vmstat(text)

    def test_duplicate_full_record_raises(self) -> None:
        text = (
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=1.00 avg60=1.00 avg300=1.00 total=1\n"
        )
        with self.assertRaises(TelemetryError):
            parse_pressure_memory(text)

    def test_nonzero_pswpin_pswpout_multiplied_by_page_size(self) -> None:
        text = "pswpin 7\npswpout 3\n"
        pswpin, pswpout = parse_vmstat(text)
        self.assertEqual((pswpin, pswpout), (7, 3))
        backend = LinuxBackend(
            reader=lambda path: {
                "/proc/vmstat": text,
                "/proc/meminfo": _fixture("proc_meminfo.txt").decode(),
                "/proc/pressure/memory": _fixture("proc_pressure_memory.txt").decode(),
            }[path],
            page_size=lambda: 4096,
        )
        health = backend.sample()
        self.assertEqual(health.swap_in_bytes, 7 * 4096)
        self.assertEqual(health.swap_out_bytes, 3 * 4096)

    def test_memavailable_missing_raises(self) -> None:
        with self.assertRaises(TelemetryError):
            parse_meminfo("MemTotal: 100 kB\n")

    def test_extra_unknown_tokens_and_records_are_ignored(self) -> None:
        text = (
            "some avg10=1.00 avg60=2.00 avg300=3.00 total=4 extra=99\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "future avg10=9.00 avg60=9.00 avg300=9.00 total=9\n"
        )
        psi = parse_pressure_memory(text)
        self.assertEqual(psi["some_avg10"], 1.0)

    def test_line_order_is_irrelevant(self) -> None:
        text = (
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "some avg10=1.00 avg60=2.00 avg300=3.00 total=4\n"
        )
        psi = parse_pressure_memory(text)
        self.assertEqual(psi["some_avg10"], 1.0)

    def test_out_of_range_percent_raises(self) -> None:
        text = (
            "some avg10=150.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        with self.assertRaises(TelemetryError):
            parse_pressure_memory(text)


class LinuxBackendTests(unittest.TestCase):
    def _fixture_reader(self):
        mapping = {
            "/proc/vmstat": _fixture("proc_vmstat.txt").decode(),
            "/proc/meminfo": _fixture("proc_meminfo.txt").decode(),
            "/proc/pressure/memory": _fixture("proc_pressure_memory.txt").decode(),
        }

        def reader(path: str) -> str:
            return mapping[path]

        return reader

    def test_sample_end_to_end_with_fixtures(self) -> None:
        backend = LinuxBackend(reader=self._fixture_reader(), page_size=lambda: 4096)
        health = backend.sample()
        self.assertEqual(health.source, constants.SOURCE_LINUX)
        self.assertEqual(health.pressure, "normal")
        self.assertEqual(health.available_bytes, 7644888 * 1024)
        self.assertEqual(health.counter_page_size_bytes, 4096)

    def test_full_avg10_over_zero_is_critical(self) -> None:
        def reader(path: str) -> str:
            if path == "/proc/pressure/memory":
                return "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.10 avg60=0.00 avg300=0.00 total=0\n"
            return self._fixture_reader()(path)

        backend = LinuxBackend(reader=reader, page_size=lambda: 4096)
        self.assertEqual(backend.sample().pressure, "critical")

    def test_some_avg10_at_and_above_threshold_is_warning(self) -> None:
        def reader(path: str) -> str:
            if path == "/proc/pressure/memory":
                return "some avg10=10.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            return self._fixture_reader()(path)

        backend = LinuxBackend(reader=reader, page_size=lambda: 4096)
        self.assertEqual(backend.sample().pressure, "warning")

    def test_some_avg10_just_below_threshold_is_normal(self) -> None:
        def reader(path: str) -> str:
            if path == "/proc/pressure/memory":
                return "some avg10=9.99 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            return self._fixture_reader()(path)

        backend = LinuxBackend(reader=reader, page_size=lambda: 4096)
        self.assertEqual(backend.sample().pressure, "normal")

    def test_configured_thresholds_are_honored(self) -> None:
        def reader(path: str) -> str:
            if path == "/proc/pressure/memory":
                return "some avg10=5.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            return self._fixture_reader()(path)

        backend = LinuxBackend(reader=reader, page_size=lambda: 4096, some_avg10_warning=5.0)
        self.assertEqual(backend.sample().pressure, "warning")

    def test_enoent_maps_to_psi_unavailable(self) -> None:
        def reader(path: str) -> str:
            if path == "/proc/pressure/memory":
                raise TelemetryError(constants.ERROR_PSI_UNAVAILABLE, "not found")
            return self._fixture_reader()(path)

        backend = LinuxBackend(reader=reader, page_size=lambda: 4096)
        with self.assertRaises(TelemetryError) as ctx:
            backend.sample()
        self.assertEqual(ctx.exception.error_code, constants.ERROR_PSI_UNAVAILABLE)

    def test_oversized_procfs_read_raises_read_error(self) -> None:
        def reader(path: str) -> str:
            if path == "/proc/vmstat":
                raise TelemetryError(constants.ERROR_READ_ERROR, "oversized")
            return self._fixture_reader()(path)

        backend = LinuxBackend(reader=reader, page_size=lambda: 4096)
        with self.assertRaises(TelemetryError) as ctx:
            backend.sample()
        self.assertEqual(ctx.exception.error_code, constants.ERROR_READ_ERROR)


class BackendSelectionTests(unittest.TestCase):
    def test_darwin_selected(self) -> None:
        from resource_controller.telemetry.darwin import DarwinBackend as Expected

        self.assertIsInstance(select_backend("darwin"), Expected)

    def test_linux_selected(self) -> None:
        from resource_controller.telemetry.linux import LinuxBackend as Expected

        self.assertIsInstance(select_backend("linux"), Expected)

    def test_unsupported_platform_raises(self) -> None:
        with self.assertRaises(TelemetryError) as ctx:
            select_backend("win32")
        self.assertEqual(ctx.exception.error_code, constants.ERROR_UNSUPPORTED_PLATFORM)


class EndToEndRegressionTests(unittest.TestCase):
    def test_divergent_psutil_vs_vm_stat_reaches_eligible_on_fifth_sample(self) -> None:
        """The bug itself: rising psutil sin/sout must never leak into the sample."""
        import psutil

        # Simulate rising psutil counters (the historical bug source) while
        # vm_stat-derived Swapins/Swapouts stay flat.
        psutil_calls = {"count": 0}

        def psutil_swap_spy():
            psutil_calls["count"] += 1
            raise AssertionError("psutil.swap_memory must never be called on Darwin")

        real_swap_memory = psutil.swap_memory
        psutil.swap_memory = psutil_swap_spy
        try:
            def runner(argv):
                if argv[0].endswith("vm_stat"):
                    return _fake_completed(stdout=_fixture("vm_stat_darwin_16k.txt"))
                return _fake_completed(stdout=b"1")

            backend = DarwinBackend(runner=runner, available_memory=lambda: 6 * GIB)
            sampler = HostSampler(
                monotonic=iter([0.0, 30.0, 60.0, 90.0, 120.0]).__next__,
                loadavg=lambda: (1.0, 1.0, 1.0),
                logical_cores=lambda: 10,
                backend=backend,
            )
            policy = AdmissionPolicy(recovery_seconds=120, max_sample_gap=35)
            results = [policy.observe(sampler.sample()) for _ in range(5)]
            self.assertFalse(results[0].eligible)
            self.assertEqual(results[0].reason, "swap-baseline")
            self.assertTrue(results[4].eligible)
            self.assertEqual(psutil_calls["count"], 0)
        finally:
            psutil.swap_memory = real_swap_memory

    def test_large_pageins_flat_swapouts_never_holds_as_swap_out(self) -> None:
        """File-page traffic (Pageins) must never surface as a swap-out hold."""
        text = _fixture("vm_stat_darwin_16k.txt").decode()
        # A synthetic second reading with Pageins way up, Swapouts flat.
        bumped = text.replace(
            "Pageins:                                   391788516.",
            "Pageins:                                   391888516.",
        )

        readings = iter([text, bumped])

        def runner(argv):
            if argv[0].endswith("vm_stat"):
                return _fake_completed(stdout=next(readings).encode())
            return _fake_completed(stdout=b"1")

        backend = DarwinBackend(runner=runner, available_memory=lambda: 6 * GIB)
        sampler = HostSampler(
            monotonic=iter([0.0, 30.0]).__next__,
            loadavg=lambda: (1.0, 1.0, 1.0),
            logical_cores=lambda: 10,
            backend=backend,
        )
        policy = AdmissionPolicy(recovery_seconds=120, max_sample_gap=35)
        first = policy.observe(sampler.sample())
        second = policy.observe(sampler.sample())
        self.assertEqual(first.reason, "swap-baseline")
        self.assertNotEqual(second.reason, "swap-out")


class DiagnosticSanitizationTests(unittest.TestCase):
    def test_diagnostic_is_single_line_and_capped(self) -> None:
        raw = ("a" * 400) + "\nline2\x01\x7f\x9f"
        err = TelemetryError("parse-error", raw)
        self.assertLessEqual(len(err.diagnostic), constants.MAX_DIAGNOSTIC_LEN)
        self.assertNotIn("\n", err.diagnostic)
        self.assertNotIn("\x01", err.diagnostic)


if __name__ == "__main__":
    unittest.main()
