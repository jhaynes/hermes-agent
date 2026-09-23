from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from resource_controller.preflight import PreflightError, read_kanban_config, verify_source_baseline
from resource_controller.supervision import CommandResult


class PreflightTests(unittest.TestCase):
    def test_raw_kanban_config_is_read_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.yaml"
            path.write_text("kanban:\n  dispatch_in_gateway: false\n  max_in_progress: 2\n")
            self.assertEqual(read_kanban_config(path)["max_in_progress"], 2)
            path.write_text("kanban:\n  dispatch_in_gateway: true\n  max_in_progress: 2\n")
            self.assertIs(read_kanban_config(path)["dispatch_in_gateway"], True)

    def test_source_baseline_uses_exact_full_sha(self) -> None:
        expected = "a" * 40
        calls: list[list[str]] = []

        def runner(argv: list[str], timeout: float, limit: int) -> CommandResult:
            calls.append(argv)
            return CommandResult(tuple(argv), 0, expected + "\n", "", False, False)

        verify_source_baseline(Path("/absolute/source"), expected, runner=runner)
        self.assertEqual(calls[0], ["/usr/bin/git", "-C", "/absolute/source", "rev-parse", "HEAD"])

        def wrong(argv: list[str], timeout: float, limit: int) -> CommandResult:
            return CommandResult(tuple(argv), 0, "b" * 40 + "\n", "", False, False)

        with self.assertRaisesRegex(PreflightError, "baseline"):
            verify_source_baseline(Path("/absolute/source"), expected, runner=wrong)


if __name__ == "__main__":
    unittest.main()
