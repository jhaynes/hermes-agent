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


if __name__ == "__main__":
    unittest.main()
