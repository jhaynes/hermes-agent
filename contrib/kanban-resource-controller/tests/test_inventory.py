from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from hermes_cli._parser import build_top_level_parser, PRE_ARGPARSE_INHERITED_FLAGS
from resource_controller.inventory import Process, WorkerParser, reconcile


class InventoryTests(unittest.TestCase):
    def test_exact_identity_counts_terminal_workers_and_holds_ambiguous_claims(self):
        parser = WorkerParser(['/runtime/python', '/runtime/hermes'],
                              build_top_level_parser()[0], PRE_ARGPARSE_INHERITED_FLAGS)
        argv = ['/runtime/python', '/runtime/hermes', '-p', 'builder', '--cli', '--accept-hooks',
                '--skills', 'x', 'chat', '-q', 'work kanban task t_12345678']
        worker = Process(101, 123.25, 1, argv)
        row = {'board': 'default', 'task_id': 't_12345678', 'profile': 'builder', 'pid': 101,
               'stamp': 12325, 'status': 'done', 'run_id': 7, 'claim_expires': None}
        result = reconcile([row], [worker], parser, now=1000)
        self.assertEqual(result['workers'][0]['task_id'], row['task_id'])
        self.assertEqual(result['holds'], [])
        self.assertTrue(reconcile([row], [replace(worker, created=124)], parser, 1000)['holds'])
        self.assertTrue(reconcile([row], [replace(worker, argv=argv[:-1] + ['work kanban task t_deadbeef'])], parser, 1000)['holds'])
        self.assertTrue(reconcile([row], [replace(worker, argv=['/usr/bin/sleep', '10'])], parser, 1000)['holds'])
        self.assertEqual(reconcile([row], [replace(worker, created=999, argv=['/usr/bin/sleep', '10'])], parser, 1000)['holds'], [])
        self.assertTrue(reconcile([], [worker], parser, 1000)['holds'])
        self.assertTrue(reconcile([{**row, 'status': 'running'}], [], parser, 1000)['holds'])
        self.assertTrue(reconcile([{**row, 'status': 'running', 'claim_expires': 999}], [worker], parser, 1000)['holds'])
        self.assertEqual(parser.parse(['/usr/bin/echo', 'work kanban task t_12345678']), None)
        self.assertEqual(parser.parse(argv[:-1] + ['please work kanban task t_12345678']), None)
        # The text is an option value, not the chat query.
        self.assertEqual(parser.parse(argv[:2] + ['-p', 'builder', '--skills', 'work kanban task t_12345678', 'chat', '-q', 'hello']), None)
        child = Process(102, 124, 101, ['/usr/bin/sleep', '10'])
        self.assertEqual(reconcile([row], [worker, child], parser, 1000)['workers'][0]['descendants'], [102])
