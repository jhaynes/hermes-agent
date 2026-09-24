from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from resource_controller.policy import HostSample
from resource_controller.runtime import RuntimeWorld
from resource_controller.spec import AdmissionCaps

GIB = 1024**3
CAPS = AdmissionCaps(2, 1, (), 1, ())


class RuntimeWorldTests(unittest.TestCase):
    def test_capture_rereads_config_estop_boards_and_processes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            db = base / "board.db"
            connection = sqlite3.connect(db)
            connection.executescript("""
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                  priority INTEGER, created_at INTEGER, worker_pid INTEGER,
                  worker_started_at TEXT, current_run_id INTEGER
                );
                CREATE TABLE task_runs (
                  id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
                  worker_pid INTEGER, worker_started_at TEXT
                );
                CREATE TABLE kanban_notify_subs (task_id TEXT, notifier_profile TEXT);
            """)
            connection.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                ("t_triage", "Need spec", None, "triage", 2, 1, None, None, None),
            )
            connection.commit()
            connection.close()
            estop = base / "ESTOP"
            estop.write_bytes(b'{"reason":"manual","engaged_at":1}')
            calls = 0

            def read_config() -> dict:
                nonlocal calls
                calls += 1
                return {
                    "dispatch_in_gateway": False,
                    "max_in_progress": 2,
                    "max_in_progress_per_profile": 1,
                    "failure_limit": 2,
                    "auto_decompose": True,
                    "reconcile_orphans": True,
                    "dispatch_stale_timeout_seconds": 0,
                    "review_dispatch": True,
                    "default_assignee": None,
                    "dispatch_profiles": ["builder"],
                }

            world = RuntimeWorld(
                boards={"alpha": db},
                admission_caps=CAPS,
                sampler=lambda: HostSample(1, 1, 10, 6 * GIB, "normal", 1, 1),
                config_reader=read_config,
                process_reader=lambda: [],
                estop_paths=(estop,),
                manual_hold=lambda: False,
            )
            first = world.capture()
            self.assertEqual(calls, 1)
            self.assertEqual(first.estop, estop.read_bytes())
            self.assertEqual(first.boards[0].triage_task, "t_triage")
            self.assertEqual(first.workers, ())
            self.assertEqual(first.boards[0].hermes_db_running_count, 0)

            estop.unlink()
            second = world.capture()
            self.assertEqual(calls, 2)
            self.assertIsNone(second.estop)
            self.assertNotEqual(first.fingerprint, second.fingerprint)

            connection = sqlite3.connect(db)
            connection.execute("UPDATE tasks SET status='running' WHERE id='t_triage'")
            connection.commit()
            connection.close()
            third = world.capture()
            self.assertEqual(third.boards[0].hermes_db_running_count, 1)
            self.assertNotEqual(second.fingerprint, third.fingerprint)

    def test_controller_caps_and_profile_registry_are_in_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            db = base / "board.db"
            connection = sqlite3.connect(db)
            connection.executescript("""
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                  priority INTEGER, created_at INTEGER, worker_pid INTEGER,
                  worker_started_at TEXT, current_run_id INTEGER
                );
                CREATE TABLE task_runs (
                  id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
                  worker_pid INTEGER, worker_started_at TEXT
                );
                CREATE TABLE kanban_notify_subs (task_id TEXT, notifier_profile TEXT);
            """)
            connection.commit()
            connection.close()

            profiles = ["builder", "reviewer"]

            def raw(host: int, profile: int) -> dict:
                return {
                    "dispatch_in_gateway": False,
                    "max_in_progress": host,
                    "max_in_progress_per_profile": profile,
                    "failure_limit": 2,
                    "auto_decompose": False,
                    "reconcile_orphans": True,
                    "dispatch_stale_timeout_seconds": 0,
                    "review_dispatch": True,
                    "default_assignee": None,
                    "dispatch_profiles": list(profiles),
                }

            def world(caps: AdmissionCaps) -> RuntimeWorld:
                return RuntimeWorld(
                    boards={"alpha": db}, admission_caps=caps,
                    sampler=lambda: HostSample(1, 1, 10, 6 * GIB, "normal", 1, 1),
                    config_reader=lambda: raw(caps.host_cap, caps.maximum_profile_cap),
                    process_reader=lambda: [], estop_paths=(), manual_hold=lambda: False,
                )

            first = world(CAPS).capture()
            changed = world(AdmissionCaps(4, 2, (), 1, ())).capture()
            self.assertNotEqual(first.fingerprint, changed.fingerprint)
            profiles.remove("reviewer")
            narrowed = world(CAPS).capture()
            self.assertNotEqual(first.fingerprint, narrowed.fingerprint)

    def test_multiple_estops_or_unreadable_path_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            missing_db = base / "missing.db"
            world = RuntimeWorld(
                boards={"alpha": missing_db},
                admission_caps=CAPS,
                sampler=lambda: HostSample(1, 1, 10, 6 * GIB, "normal", 1, 1),
                config_reader=lambda: {},
                process_reader=lambda: [],
                estop_paths=(),
                manual_hold=lambda: False,
            )
            with self.assertRaises(Exception):
                world.capture()


if __name__ == "__main__":
    unittest.main()
