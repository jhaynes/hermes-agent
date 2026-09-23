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


if __name__ == "__main__":
    unittest.main()
