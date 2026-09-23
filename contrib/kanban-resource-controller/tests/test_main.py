from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from resource_controller.main import build_parser, set_manual_hold
from resource_controller.storage import SecureStateStore


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
