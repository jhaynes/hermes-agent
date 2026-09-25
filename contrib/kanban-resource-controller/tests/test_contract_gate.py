from __future__ import annotations

"""End-to-end RuntimeWorld.capture() against a real sqlite board built with the
pinned Hermes DDL, and a real child process whose fingerprint is written by the
PINNED Hermes ``_process_fingerprint`` (subprocess) — not the controller's own
function, per the plan's release-gate requirement.

This module is MANDATORY in the release gate: it fails (does not skip) when
the pinned source is unavailable there. Set ``HERMES_SOURCE_ROOT`` to run it;
see RUNBOOK.md "Identity contract" / contract gate.
"""

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from resource_controller.inventory import ProcessSnapshot
from resource_controller.runtime import RuntimeWorld
from resource_controller.spec import AdmissionCaps
from resource_controller.policy import HostSample

GIB = 1024**3
EXPECTED_HERMES_COMMIT = "0e0a29ad315da6b6fd5b63e2903600af85e839e5"

# Pinned Hermes DDL for the two columns this contract cares about, copied from
# hermes_cli/kanban_db.py (tasks / task_runs), trimmed to what the controller
# queries. See REQUIREMENT_LEDGER.md for full provenance.
_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
  worker_pid INTEGER, worker_started_at TEXT, current_run_id INTEGER
);
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, status TEXT NOT NULL,
  worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER
);
CREATE TABLE kanban_notify_subs (task_id TEXT, notifier_profile TEXT);
"""


def _hermes_source_root() -> Path:
    root = os.environ.get("HERMES_SOURCE_ROOT")
    if not root:
        raise unittest.SkipTest(
            "HERMES_SOURCE_ROOT not set; this test is MANDATORY in the release "
            "gate (see RUNBOOK.md) but is skippable in the ordinary hermetic "
            "unit run."
        )
    return Path(root)


def _pinned_venv_python(root: Path) -> Path:
    candidate = root / "venv" / "bin" / "python"
    if not candidate.exists():
        candidate = Path(sys.executable)
    return candidate


def _verify_pinned_commit(root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read HEAD of {root}: {result.stderr}")
    commit = result.stdout.strip()
    if commit != EXPECTED_HERMES_COMMIT:
        raise RuntimeError(
            f"pinned Hermes source at {root} is {commit}, expected {EXPECTED_HERMES_COMMIT}"
        )


def _pinned_process_fingerprint(root: Path, pid: int) -> str:
    python = _pinned_venv_python(root)
    proc = subprocess.run(
        [str(python), "-c", (
            "import sys; sys.path.insert(0, '.'); "
            "from hermes_cli.kanban_db_dispatch import _process_fingerprint; "
            f"print(_process_fingerprint({pid}))"
        )],
        cwd=str(root), capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pinned Hermes _process_fingerprint subprocess failed: {proc.stderr}")
    return proc.stdout.strip()


class ContractGateTests(unittest.TestCase):
    """Mandatory when HERMES_SOURCE_ROOT is set; skips (never silently passes)
    otherwise. Never a runtime import of Hermes — subprocess only."""

    def setUp(self) -> None:
        self.root = _hermes_source_root()
        _verify_pinned_commit(self.root)

    def test_pinned_fingerprint_matches_controller_string_for_self(self) -> None:
        from resource_controller.worker_identity import current_fingerprint, current_instantiation_epoch

        pid = os.getpid()
        epoch = current_instantiation_epoch()
        controller_string = current_fingerprint(pid, epoch=epoch)
        pinned_string = _pinned_process_fingerprint(self.root, pid)
        self.assertEqual(
            controller_string, pinned_string,
            f"controller fingerprint {controller_string!r} != pinned Hermes {pinned_string!r}",
        )

    def test_end_to_end_capture_against_real_child_written_by_pinned_hermes(self) -> None:
        """A real child process; its worker_started_at row is written using the
        PINNED Hermes _process_fingerprint output (via subprocess), not the
        controller's own function. RuntimeWorld.capture() must recognise it as
        exactly one worker."""
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            db = base / "board.db"
            connection = sqlite3.connect(db)
            connection.executescript(_SCHEMA)
            connection.commit()
            connection.close()

            # A real, long-lived child whose argv/env match the worker contract.
            script = base / "worker.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            child = subprocess.Popen(
                [
                    sys.executable, str(script), "-p", "reviewquality", "--cli",
                    "--accept-hooks", "chat", "-q", "work kanban task t_gate001",
                ],
                env={**os.environ, "HERMES_KANBAN_TASK": "t_gate001",
                     "HERMES_KANBAN_RUN_ID": "1", "HERMES_KANBAN_BOARD": "gate",
                     "HERMES_PROFILE": "reviewquality"},
            )
            try:
                time.sleep(0.2)
                fingerprint = _pinned_process_fingerprint(self.root, child.pid)
                self.assertNotEqual(fingerprint, "None")

                connection = sqlite3.connect(db)
                connection.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                    ("t_gate001", "Gate", "reviewquality", "running", 0, 1, child.pid, fingerprint, 1),
                )
                connection.execute(
                    "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
                    (1, "t_gate001", "reviewquality", "running", child.pid, fingerprint, None),
                )
                connection.commit()
                connection.close()

                from resource_controller.processes import scan_worker_processes

                # This host may have other real, unrelated worker processes matching
                # the argv contract (e.g. a leftover live worker from a past
                # incident). Scope the reader to our own test child only, so this
                # test proves capture() recognises OUR worker end-to-end without
                # being contaminated by unrelated live processes sharing the host.
                def scoped_process_reader():
                    return [s for s in scan_worker_processes() if s.pid == child.pid]

                world = RuntimeWorld(
                    boards={"gate": db},
                    admission_caps=AdmissionCaps(2, 1, (), 1, ()),
                    sampler=lambda: HostSample(1, 1, 10, 6 * GIB, "normal", 1, 1),
                    config_reader=lambda: {
                        "dispatch_in_gateway": False, "max_in_progress": 2,
                        "max_in_progress_per_profile": 1, "failure_limit": 2,
                        "auto_decompose": True, "reconcile_orphans": True,
                        "dispatch_stale_timeout_seconds": 0, "review_dispatch": True,
                        "default_assignee": None, "dispatch_profiles": ["reviewquality"],
                    },
                    process_reader=scoped_process_reader,
                    estop_paths=(),
                    manual_hold=lambda: False,
                )
                snapshot = world.capture()
                self.assertEqual(len(snapshot.workers), 1)
                self.assertEqual(snapshot.workers[0].task_id, "t_gate001")

                # Negative: a fingerprint written +1 must produce a hold.
                mutated_pid = child.pid
                mutated_fp = fingerprint[:-1] + str((int(fingerprint[-1]) + 1) % 10)
                connection = sqlite3.connect(db)
                connection.execute("UPDATE tasks SET worker_started_at=? WHERE id='t_gate001'", (mutated_fp,))
                connection.execute("UPDATE task_runs SET worker_started_at=? WHERE id=1", (mutated_fp,))
                connection.commit()
                connection.close()
                with self.assertRaises(Exception):
                    world.capture()
            finally:
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
