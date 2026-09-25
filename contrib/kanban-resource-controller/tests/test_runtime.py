from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from resource_controller.inventory import ProcessSnapshot, admission_capacity
from resource_controller.policy import HostSample
from resource_controller.runtime import RuntimeWorld
from resource_controller.spec import AdmissionCaps
from resource_controller.worker_identity import IdentityHold

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
                  worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER
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
                  worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER
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


class FinishingWorkerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = self.base / "board.db"
        connection = sqlite3.connect(self.db)
        connection.executescript("""
            CREATE TABLE tasks (
              id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
              priority INTEGER, created_at INTEGER, worker_pid INTEGER,
              worker_started_at TEXT, current_run_id INTEGER
            );
            CREATE TABLE task_runs (
              id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
              worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER
            );
            CREATE TABLE kanban_notify_subs (task_id TEXT, notifier_profile TEXT);
        """)
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            ("t_live", "Build", "builder", "running", 0, 1, 42, "epoch|1234", 7),
        )
        connection.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
            (7, "t_live", "builder", "running", 42, "epoch|1234", None),
        )
        connection.commit()
        connection.close()
        self.processes = [self._process()]
        self.manual_error = False

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _process(self, **changes) -> ProcessSnapshot:
        environment = {
            "HERMES_KANBAN_TASK": "t_live",
            "HERMES_KANBAN_RUN_ID": "7",
            "HERMES_KANBAN_BOARD": "alpha",
            "HERMES_PROFILE": "builder",
        }
        values = {
            "pid": 42,
            "created_at": 12.34,
            "ppid": 1,
            "argv": (
                "/python", "-m", "hermes_cli.main", "-p", "builder", "--cli",
                "--accept-hooks", "chat", "-q", "work kanban task t_live", "-Q",
            ),
            "environment": environment,
            "accessible": True,
            "start_fingerprint": 1234,
        }
        values.update(changes)
        return ProcessSnapshot(**values)

    def _config(self) -> dict:
        return {
            "dispatch_in_gateway": False, "max_in_progress": 2,
            "max_in_progress_per_profile": 1, "failure_limit": 2,
            "auto_decompose": False, "reconcile_orphans": True,
            "dispatch_stale_timeout_seconds": 0, "review_dispatch": True,
            "default_assignee": None, "dispatch_profiles": ["builder"],
        }

    def _world(self) -> RuntimeWorld:
        return RuntimeWorld(
            boards={"alpha": self.db}, admission_caps=CAPS,
            sampler=lambda: HostSample(1, 1, 10, 6 * GIB, "normal", 1, 1),
            config_reader=self._config, process_reader=lambda: tuple(self.processes),
            estop_paths=(), manual_hold=self._manual_hold,
        )

    def _manual_hold(self) -> bool:
        if self.manual_error:
            raise RuntimeError("manual hold read failed")
        return False

    def _end_run(self, *, ended_at=123) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE tasks SET status='done', worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL WHERE id='t_live'"
        )
        connection.execute(
            "UPDATE task_runs SET status='done', ended_at=? WHERE id=7", (ended_at,),
        )
        connection.commit()
        connection.close()

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_second_capture_keeps_just_ended_worker_counted(self, _epoch) -> None:
        world = self._world()
        active = world.capture().workers[0]
        self._end_run()
        finishing = world.capture().workers[0]
        self.assertEqual(
            (finishing.board, finishing.task_id, finishing.run_id, finishing.pid,
             finishing.created_at, finishing.profile, finishing.worker_fingerprint),
            (active.board, active.task_id, active.run_id, active.pid,
             active.created_at, active.profile, active.worker_fingerprint),
        )
        self.assertEqual(finishing.task_status, "done")
        for board, profile in (("alpha", "reviewer"), ("other", "builder")):
            self.assertFalse(
                admission_capacity((finishing,), caps=CAPS, board=board, profile=profile).available
            )

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_consecutive_active_captures_do_not_create_ended_evidence_or_hold(self, _epoch) -> None:
        world = self._world()
        identities = []
        for _ in range(4):
            worker = world.capture().workers[0]
            identities.append((worker.task_id, worker.run_id, worker.pid, worker.worker_fingerprint))
        self.assertEqual(identities, [identities[0]] * 4)

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_ended_worker_without_previous_capture_holds(self, _epoch) -> None:
        self._end_run()
        with self.assertRaisesRegex(IdentityHold, "no canonical run"):
            self._world().capture()

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_previous_identity_is_not_shared_with_a_fresh_runtime_world(self, _epoch) -> None:
        original = self._world()
        original.capture()
        self._end_run()
        with self.assertRaisesRegex(IdentityHold, "no canonical run"):
            self._world().capture()

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_previous_identity_mismatch_or_unended_run_holds(self, _epoch) -> None:
        cases = ("fingerprint", "argv", "environment", "not-ended")
        for case in cases:
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                world = self._world()
                world.capture()
                self._end_run(ended_at=None if case == "not-ended" else 123)
                if case == "fingerprint":
                    self.processes[:] = [self._process(start_fingerprint=9999)]
                elif case == "argv":
                    argv = list(self._process().argv)
                    argv[argv.index("builder")] = "reviewer"
                    self.processes[:] = [self._process(argv=tuple(argv))]
                elif case == "environment":
                    env = dict(self._process().environment)
                    env["HERMES_KANBAN_RUN_ID"] = "8"
                    self.processes[:] = [self._process(environment=env)]
                with self.assertRaises(IdentityHold):
                    world.capture()

    @mock.patch("resource_controller.runtime.current_instantiation_epoch", return_value="epoch")
    def test_failed_capture_does_not_poison_previous_identity_cache(self, _epoch) -> None:
        world = self._world()
        world.capture()
        connection = sqlite3.connect(self.db)
        connection.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
            (8, "t_live", "builder", "running", 42, "epoch|1234", None),
        )
        connection.execute("UPDATE tasks SET current_run_id=8 WHERE id='t_live'")
        connection.commit()
        connection.close()
        env = dict(self._process().environment)
        env["HERMES_KANBAN_RUN_ID"] = "8"
        self.processes[:] = [self._process(environment=env)]
        self.manual_error = True
        with self.assertRaisesRegex(RuntimeError, "manual hold"):
            world.capture()
        self.manual_error = False
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE tasks SET status='done', worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL WHERE id='t_live'"
        )
        connection.execute("UPDATE task_runs SET status='done', ended_at=123 WHERE id=8")
        connection.commit()
        connection.close()
        with self.assertRaises(IdentityHold):
            world.capture()


if __name__ == "__main__":
    unittest.main()
