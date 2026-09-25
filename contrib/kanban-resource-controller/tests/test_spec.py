from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from resource_controller.spec import RuntimeSpec, SpecError


class RuntimeSpecTests(unittest.TestCase):
    def test_absolute_external_runtime_spec_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / "source"
            source.mkdir()
            config = base / "controller.json"
            config.write_text(json.dumps({
                "hermes_executable": str(base / "bin" / "hermes"),
                "hermes_home": str(base / "home"),
                "source_root": str(source),
                "expected_source_commit": "a" * 40,
                "dispatcher_lock": str(base / "home" / "kanban" / ".dispatcher.lock"),
                "state_dir": str(base / "operations" / "state"),
                "boards": {"default": str(base / "home" / "kanban.db")},
                "interval_seconds": 30,
            }))
            config.chmod(0o600)
            spec = RuntimeSpec.read(config)
            self.assertEqual(spec.expected_source_commit, "a" * 40)
            self.assertEqual(spec.interval_seconds, 30)
            self.assertEqual(spec.admission_caps.host_cap, 2)
            self.assertEqual(spec.admission_caps.profile_cap, 1)
            self.assertEqual(spec.admission_caps.board_cap, 1)

    def test_nondefault_admission_caps_and_overrides_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / "source"
            source.mkdir()
            config = base / "controller.json"
            config.write_text(json.dumps({
                "hermes_executable": str(base / "hermes"),
                "hermes_home": str(base / "home"),
                "source_root": str(source),
                "expected_source_commit": "a" * 40,
                "dispatcher_lock": str(base / "lock"),
                "state_dir": str(base / "state"),
                "boards": {"default": str(base / "default.db"), "smithers": str(base / "smithers.db")},
                "interval_seconds": 30,
                "admission": {
                    "host_cap": 4,
                    "profile_cap": 1,
                    "profile_overrides": {"builder": 2},
                    "board_cap": 1,
                    "board_overrides": {"smithers": 4},
                },
            }))
            config.chmod(0o600)
            caps = RuntimeSpec.read(config).admission_caps
            self.assertEqual(caps.for_profile("builder"), 2)
            self.assertEqual(caps.for_profile("reviewer"), 1)
            self.assertEqual(caps.for_board("smithers"), 4)
            self.assertEqual(caps.for_board("default"), 1)

    def test_invalid_admission_values_and_unknown_boards_are_refused(self) -> None:
        invalid = [
            None,
            {"host_cap": 0},
            {"host_cap": True},
            {"profile_cap": 5},
            {"profile_overrides": {"builder": 5}},
            {"board_overrides": {"unknown": 2}},
            {"profile_overrides": []},
            {"extra": 1},
        ]
        for admission in invalid:
            with self.subTest(admission=admission), tempfile.TemporaryDirectory() as root:
                base = Path(root)
                source = base / "source"
                source.mkdir()
                raw = {
                    "hermes_executable": str(base / "hermes"),
                    "hermes_home": str(base / "home"),
                    "source_root": str(source),
                    "expected_source_commit": "a" * 40,
                    "dispatcher_lock": str(base / "lock"),
                    "state_dir": str(base / "state"),
                    "boards": {"default": str(base / "default.db")},
                    "interval_seconds": 30,
                    "admission": None if admission is None else {
                        "host_cap": 4, "profile_cap": 1,
                        "profile_overrides": {}, "board_cap": 1, "board_overrides": {},
                        **admission,
                    },
                }
                path = base / "controller.json"
                path.write_text(json.dumps(raw))
                path.chmod(0o600)
                with self.assertRaises(SpecError):
                    RuntimeSpec.read(path)

    def test_relative_or_in_source_state_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / "source"
            source.mkdir()
            common = {
                "hermes_executable": str(base / "hermes"),
                "hermes_home": str(base / "home"),
                "source_root": str(source),
                "expected_source_commit": "b" * 40,
                "dispatcher_lock": str(base / "lock"),
                "state_dir": str(source / "state"),
                "boards": {"default": str(base / "db")},
                "interval_seconds": 30,
            }
            config = base / "bad.json"
            config.write_text(json.dumps(common))
            config.chmod(0o600)
            with self.assertRaisesRegex(SpecError, "outside"):
                RuntimeSpec.read(config)
            common["state_dir"] = "relative"
            config.write_text(json.dumps(common))
            with self.assertRaisesRegex(SpecError, "absolute"):
                RuntimeSpec.read(config)


class TelemetrySpecTests(unittest.TestCase):
    def _write(self, base: Path, extra: dict | None = None) -> Path:
        source = base / "source"
        source.mkdir(exist_ok=True)
        raw = {
            "hermes_executable": str(base / "hermes"),
            "hermes_home": str(base / "home"),
            "source_root": str(source),
            "expected_source_commit": "c" * 40,
            "dispatcher_lock": str(base / "lock"),
            "state_dir": str(base / "state"),
            "boards": {"default": str(base / "db")},
            "interval_seconds": 30,
        }
        raw.update(extra or {})
        path = base / "controller.json"
        path.write_text(json.dumps(raw))
        path.chmod(0o600)
        return path

    def test_absent_telemetry_section_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spec = RuntimeSpec.read(self._write(Path(root)))
            self.assertEqual(spec.linux_psi_some_avg10_warning, 10.0)
            self.assertEqual(spec.linux_psi_full_avg10_critical, 0.0)

    def test_configured_thresholds_are_read_from_controller_json(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spec = RuntimeSpec.read(self._write(Path(root), {
                "telemetry": {"linux_psi": {"some_avg10_warning": 25, "full_avg10_critical": 1.5}},
            }))
            self.assertEqual(spec.linux_psi_some_avg10_warning, 25.0)
            self.assertEqual(spec.linux_psi_full_avg10_critical, 1.5)

    def test_invalid_or_unknown_telemetry_settings_refuse(self) -> None:
        bad = [
            {"telemetry": []},
            {"telemetry": {"other": {}}},
            {"telemetry": {"linux_psi": {"typo": 1}}},
            {"telemetry": {"linux_psi": {"some_avg10_warning": True}}},
            {"telemetry": {"linux_psi": {"some_avg10_warning": "10"}}},
            {"telemetry": {"linux_psi": {"some_avg10_warning": 101}}},
            {"telemetry": {"linux_psi": {"full_avg10_critical": -0.1}}},
        ]
        for extra in bad:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(SpecError):
                    RuntimeSpec.read(self._write(Path(root), extra))

    def test_absent_pacing_section_uses_declared_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spec = RuntimeSpec.read(self._write(Path(root)))
            self.assertEqual((spec.recovery_seconds, spec.max_sample_gap_seconds), (120.0, 35.0))
            self.assertEqual(spec.interval_seconds, 30)

    def test_pacing_and_interval_are_read_from_controller_json(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spec = RuntimeSpec.read(self._write(Path(root), {
                "interval_seconds": 10,
                "pacing": {"recovery_seconds": 5, "max_sample_gap_seconds": 25.5},
            }))
            self.assertEqual(spec.interval_seconds, 10)
            self.assertEqual(spec.recovery_seconds, 5.0)
            self.assertEqual(spec.max_sample_gap_seconds, 25.5)
            zero = RuntimeSpec.read(self._write(Path(root), {
                "interval_seconds": 10,
                "pacing": {"recovery_seconds": 0, "max_sample_gap_seconds": 11},
            }))
            self.assertEqual(zero.recovery_seconds, 0.0)

    def test_interval_upper_bound_is_exact(self) -> None:
        # A wide gap isolates the interval bound from the gap-must-exceed-interval rule.
        wide = {"recovery_seconds": 5, "max_sample_gap_seconds": 400}
        with tempfile.TemporaryDirectory() as root:
            spec = RuntimeSpec.read(self._write(Path(root), {"interval_seconds": 300, "pacing": wide}))
            self.assertEqual(spec.interval_seconds, 300)
            with self.assertRaisesRegex(SpecError, "interval_seconds"):
                RuntimeSpec.read(self._write(Path(root), {"interval_seconds": 301, "pacing": wide}))
            with self.assertRaisesRegex(SpecError, "interval_seconds"):
                RuntimeSpec.read(self._write(Path(root), {"interval_seconds": 0, "pacing": wide}))
            self.assertEqual(
                RuntimeSpec.read(self._write(Path(root), {"interval_seconds": 1, "pacing": wide})).interval_seconds, 1,
            )

    def test_invalid_pacing_or_interval_refuses(self) -> None:
        bad = [
            {"interval_seconds": 0},
            {"interval_seconds": 301},
            {"interval_seconds": True},
            {"interval_seconds": 10.0},
            {"pacing": []},
            {"pacing": {"recovery_seconds": 5}},
            {"pacing": {"recovery_seconds": 5, "max_sample_gap_seconds": 45, "typo": 1}},
            {"pacing": {"recovery_seconds": -1, "max_sample_gap_seconds": 45}},
            {"pacing": {"recovery_seconds": True, "max_sample_gap_seconds": 45}},
            {"pacing": {"recovery_seconds": "5", "max_sample_gap_seconds": 45}},
            {"pacing": {"recovery_seconds": 3601, "max_sample_gap_seconds": 45}},
            {"pacing": {"recovery_seconds": float("nan"), "max_sample_gap_seconds": 45}},
            # the gap must exceed the loop interval, or every sample would be a gap
            {"pacing": {"recovery_seconds": 5, "max_sample_gap_seconds": 30}},
            {"interval_seconds": 40},  # default 35 s gap would not exceed a 40 s interval
        ]
        for extra in bad:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(SpecError):
                    RuntimeSpec.read(self._write(Path(root), extra))


if __name__ == "__main__":
    unittest.main()
