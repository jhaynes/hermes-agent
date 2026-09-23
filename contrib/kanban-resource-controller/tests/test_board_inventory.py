from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from resource_controller.board_inventory import BoardInventoryError, read_board_inventory


_SCHEMA = """
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, status TEXT NOT NULL,
 priority INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
 worker_pid INTEGER, worker_started_at INTEGER, current_run_id INTEGER
);
CREATE TABLE task_runs (
 id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, status TEXT NOT NULL
);
CREATE TABLE kanban_notify_subs (task_id TEXT, notifier_profile TEXT);
"""


class BoardInventoryTests(unittest.TestCase):
    def make_db(self, root: str) -> Path:
        path = Path(root) / "kanban.db"
        connection = sqlite3.connect(path)
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            ("t_live", "Build", "builder", "done", 0, 1, 42, 1000, 7),
        )
        connection.execute("INSERT INTO task_runs VALUES (?,?,?,?)", (7, "t_live", "builder", "running"))
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
            self.assertEqual(snapshot.unowned_subscriptions, 0)

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


if __name__ == "__main__":
    unittest.main()
