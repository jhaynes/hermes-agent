from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from resource_controller.board_inventory import BoardInventoryError, read_board_inventory
from resource_controller.inventory import LiveWorker


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

FINGERPRINT = "|179027411681"


class BoardInventoryTests(unittest.TestCase):
    def make_db(self, root: str) -> Path:
        path = Path(root) / "kanban.db"
        connection = sqlite3.connect(path)
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            ("t_live", "Build", "builder", "done", 0, 1, 42, FINGERPRINT, 7),
        )
        connection.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
            (7, "t_live", "builder", "running", 42, FINGERPRINT, None),
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            ("t_ready", "Review changes", "reviewscope", "ready", 0, 2, None, None, None),
        )
        connection.execute("INSERT INTO kanban_notify_subs VALUES (?,?)", ("t_ready", "builder"))
        connection.commit()
        connection.close()
        return path

    def test_read_only_inventory_returns_canonical_identity_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot = read_board_inventory(path, board="alpha", max_rows=10)
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(snapshot.titles, {"t_ready": "Review changes"})
            self.assertEqual(snapshot.assignees, {"t_ready": "reviewscope"})
            self.assertEqual(snapshot.runs[0].task_status, "done")
            self.assertEqual(snapshot.hermes_db_running_count, 0)
            self.assertEqual(snapshot.runs[0].worker_fingerprint.raw, FINGERPRINT)
            self.assertEqual(snapshot.unowned_subscriptions, 0)

            connection = sqlite3.connect(path)
            connection.execute("UPDATE tasks SET status='running' WHERE id='t_live'")
            connection.commit()
            connection.close()
            self.assertEqual(
                read_board_inventory(path, board="alpha", max_rows=10).hermes_db_running_count,
                1,
            )

    def test_unowned_subscription_and_schema_or_row_drift_hold(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO kanban_notify_subs VALUES (?,?)", ("t_ready", ""))
            connection.commit()
            connection.close()
            snapshot = read_board_inventory(path, board="alpha", max_rows=10)
            self.assertEqual(snapshot.unowned_subscriptions, 1)
            with self.assertRaisesRegex(BoardInventoryError, "row bound"):
                read_board_inventory(path, board="alpha", max_rows=0)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.db"
            sqlite3.connect(path).execute("CREATE TABLE tasks (id TEXT)").connection.close()
            with self.assertRaisesRegex(BoardInventoryError, "schema"):
                read_board_inventory(path, board="alpha", max_rows=10)

    def test_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            link = Path(root) / "linked.db"
            link.symlink_to(path)
            with self.assertRaisesRegex(BoardInventoryError, "symlink"):
                read_board_inventory(link, board="alpha", max_rows=10)

    def test_null_fingerprint_with_live_pid_holds(self) -> None:
        for table, where in (("tasks", "id='t_live'"), ("task_runs", "id=7")):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as root:
                path = self.make_db(root)
                connection = sqlite3.connect(path)
                connection.execute(f"UPDATE {table} SET worker_started_at=NULL WHERE {where}")
                connection.commit()
                connection.close()
                with self.assertRaisesRegex(BoardInventoryError, "incomplete active identity for t_live"):
                    read_board_inventory(path, board="alpha", max_rows=10)

    def test_unverified_fingerprint_holds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE tasks SET worker_started_at='unverified' WHERE id='t_live'"
            )
            connection.execute(
                "UPDATE task_runs SET worker_started_at='unverified' WHERE id=7"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(BoardInventoryError, "board inventory failed"):
                read_board_inventory(path, board="alpha", max_rows=10)

    def test_tasks_vs_task_runs_fingerprint_conflict_holds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE task_runs SET worker_started_at='|999999' WHERE id=7"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(BoardInventoryError, "identity-conflict"):
                read_board_inventory(path, board="alpha", max_rows=10)

    def test_tasks_vs_task_runs_pid_conflict_holds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            connection = sqlite3.connect(path)
            connection.execute("UPDATE task_runs SET worker_pid=99 WHERE id=7")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(BoardInventoryError, "identity-conflict"):
                read_board_inventory(path, board="alpha", max_rows=10)

    def test_malformed_fingerprint_holds_not_bare_valueerror(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            connection = sqlite3.connect(path)
            connection.execute("UPDATE tasks SET worker_started_at=1000 WHERE id='t_live'")
            connection.execute("UPDATE task_runs SET worker_started_at=1000 WHERE id=7")
            connection.commit()
            connection.close()
            with self.assertRaises(BoardInventoryError) as ctx:
                read_board_inventory(path, board="alpha", max_rows=10)
            self.assertNotEqual(type(ctx.exception.__cause__), ValueError)

    def test_ended_run_projection_requires_ended_at_and_retained_exact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            prior = LiveWorker("alpha", "t_live", 7, 42, 1.0, "builder", "running", FINGERPRINT)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE tasks SET worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL WHERE id='t_live'"
            )
            connection.commit()
            connection.close()
            snapshot = read_board_inventory(
                path, board="alpha", max_rows=10, ended_candidates=(prior,),
            )
            self.assertEqual(snapshot.ended_runs, ())

            connection = sqlite3.connect(path)
            connection.execute("UPDATE task_runs SET status='done', ended_at=123 WHERE id=7")
            connection.commit()
            connection.close()
            ended = read_board_inventory(
                path, board="alpha", max_rows=10, ended_candidates=(prior,),
            ).ended_runs
            self.assertEqual(len(ended), 1)
            self.assertEqual(
                (ended[0].board, ended[0].task_id, ended[0].run_id, ended[0].profile,
                 ended[0].pid, ended[0].worker_fingerprint.raw, ended[0].run_status, ended[0].ended_at),
                ("alpha", "t_live", 7, "builder", 42, FINGERPRINT, "done", 123),
            )

    def test_unrelated_legacy_or_malformed_ended_history_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            prior = LiveWorker("alpha", "t_live", 7, 42, 1.0, "builder", "running", FINGERPRINT)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE tasks SET worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL WHERE id='t_live'"
            )
            connection.execute("UPDATE task_runs SET status='done', ended_at=123 WHERE id=7")
            for index, fingerprint in enumerate((None, "unverified", "malformed"), start=20):
                connection.execute(
                    "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
                    (index, f"t_old{index}", "builder", "done", 42, fingerprint, 1),
                )
            connection.commit()
            connection.close()
            snapshot = read_board_inventory(
                path, board="alpha", max_rows=2, ended_candidates=(prior,),
            )
            self.assertEqual(tuple(run.run_id for run in snapshot.ended_runs), (7,))

    def test_matching_malformed_or_duplicate_ended_identity_holds(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            prior = LiveWorker("alpha", "t_live", 7, 42, 1.0, "builder", "running", FINGERPRINT)
            connection = sqlite3.connect(path)
            connection.execute(
                "UPDATE tasks SET worker_pid=NULL, worker_started_at=NULL, current_run_id=NULL WHERE id='t_live'"
            )
            connection.execute(
                "UPDATE task_runs SET status='done', ended_at=123, worker_started_at='unverified' WHERE id=7"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(BoardInventoryError, "ended identity"):
                read_board_inventory(path, board="alpha", max_rows=10, ended_candidates=(prior,))

        with tempfile.TemporaryDirectory() as root:
            path = self.make_db(root)
            prior = LiveWorker("alpha", "t_live", 7, 42, 1.0, "builder", "running", FINGERPRINT)
            with self.assertRaisesRegex(BoardInventoryError, "duplicate ended candidate"):
                read_board_inventory(
                    path, board="alpha", max_rows=10, ended_candidates=(prior, prior),
                )


if __name__ == "__main__":
    unittest.main()
