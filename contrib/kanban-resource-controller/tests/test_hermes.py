from pathlib import Path
import sqlite3
import tempfile
import unittest

from resource_controller.hermes import config_holds, read_board


class HermesTests(unittest.TestCase):
    def test_contract_refuses_unsupported_settings_and_reads_canonical_runs_without_mutation(self):
        cfg = {'dispatch_in_gateway': False, 'max_in_progress': 2,
               'max_in_progress_per_profile': 1, 'dispatch_stale_timeout_seconds': 0,
               'reconcile_orphans': True, 'auto_decompose': True, 'failure_limit': 2}
        self.assertEqual(config_holds(cfg), [])
        for key, value in [('dispatch_in_gateway', True), ('dispatch_stale_timeout_seconds', 14400),
                           ('reconcile_orphans', False), ('max_in_progress', 5),
                           ('max_in_progress_per_profile', None), ('auto_decompose', 'false')]:
            self.assertTrue(config_holds({**cfg, key: value}), key)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'kanban.db'
            conn = sqlite3.connect(db)
            conn.executescript('''
                CREATE TABLE tasks(id TEXT, assignee TEXT, status TEXT, current_run_id INT,
                  claim_lock TEXT, claim_expires INT, priority INT, created_at INT);
                CREATE TABLE task_runs(id INT, task_id TEXT, profile TEXT, status TEXT,
                  worker_pid INT, claim_expires INT);
                CREATE TABLE task_events(id INT, task_id TEXT, run_id INT, kind TEXT, payload TEXT);
                CREATE TABLE kanban_notify_subs(notifier_profile TEXT);
                INSERT INTO tasks VALUES('t_12345678','builder','done',NULL,NULL,NULL,0,1);
                INSERT INTO task_runs VALUES(9,'t_12345678','builder','done',101,NULL);
                INSERT INTO task_events VALUES(1,'t_12345678',9,'spawned','{"pid":101,"started_at":12325}');
            ''')
            conn.commit()
            before = db.read_bytes()
            result = read_board(db, 'default')
            self.assertEqual(result['runs'][0]['stamp'], 12325)
            self.assertEqual(result['holds'], [])
            self.assertEqual(db.read_bytes(), before)
            conn.execute("INSERT INTO task_runs VALUES(10,'t_deadbeef','builder','running',102,9999)")
            conn.commit()
            self.assertIn('identity:run-task-mismatch:default:10', read_board(db, 'default')['holds'])
            conn.execute('INSERT INTO kanban_notify_subs VALUES(NULL)')
            conn.commit()
            self.assertIn('compatibility:unowned-notifications:default', read_board(db, 'default')['holds'])
            conn.execute('DROP TABLE task_runs')
            conn.commit()
            with self.assertRaises(ValueError):
                read_board(db, 'default')
            conn.close()
