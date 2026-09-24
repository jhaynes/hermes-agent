from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

from resource_controller.cli_adapter import CliCommands
from resource_controller.engine import BoardView
from resource_controller.priority import PredictedPick


class SandboxedCliTests(unittest.TestCase):
    def test_supported_commands_execute_only_against_a_fake_cli(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            log_path = root_path / "argv.jsonl"
            executable = root_path / "fake-hermes"
            executable.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import pathlib
                    import sys

                    log = pathlib.Path({str(log_path)!r})
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(sys.argv[1:]) + "\\n")
                    dispatch = {{
                        "reclaimed": 0, "crashed": [], "timed_out": [], "stale": [],
                        "auto_blocked": [], "promoted": 0,
                        "spawned": [{{"task_id": "t_pred", "assignee": "reviewquality", "workspace": "scratch"}}],
                        "skipped_unassigned": [], "skipped_nonspawnable": [],
                        "skipped_per_profile_capped": [], "auto_assigned_default": [],
                        "reaped_terminal_workers": [], "respawn_guarded": [], "rate_limited": [],
                        "skipped_locked": False, "memory_pressure": None,
                    }}
                    if "decompose" in sys.argv:
                        task_id = sys.argv[sys.argv.index("decompose") + 1]
                        print(json.dumps({{"task_id": task_id, "ok": True, "reason": "ok", "fanout": 1,
                                          "child_ids": ["t_child"], "new_title": None}}))
                    else:
                        print(json.dumps(dispatch))
                    """
                ),
                encoding="utf-8",
            )
            executable.chmod(0o700)

            def reconcile(board: str, task_id: str) -> str | None:
                return None if task_id == "t_triage" else task_id

            commands = CliCommands(executable, reconcile_actual=reconcile)
            board = BoardView("sandbox", {"t_pred": "Review"}, {"t_pred": "reviewquality"}, None)
            prediction = commands.predict(board, failure_limit=2)
            self.assertEqual(prediction, PredictedPick("sandbox", "t_pred", "reviewquality", "Review"))
            self.assertEqual(commands.dispatch(prediction, failure_limit=2).actual_task_id, "t_pred")
            self.assertEqual(commands.decompose("sandbox", "t_triage").actual_task_id, "t_triage")

            calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                calls,
                [
                    ["kanban", "--board", "sandbox", "dispatch", "--dry-run", "--max", "1", "--failure-limit", "2", "--json"],
                    ["kanban", "--board", "sandbox", "dispatch", "--max", "1", "--failure-limit", "2", "--json"],
                    ["kanban", "--board", "sandbox", "decompose", "t_triage", "--author", "auto-decomposer", "--json"],
                ],
            )
            self.assertFalse(any("--all" in call for call in calls))


if __name__ == "__main__":
    unittest.main()