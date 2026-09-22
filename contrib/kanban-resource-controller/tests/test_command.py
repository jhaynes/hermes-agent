import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from resource_controller.command import Command, Uncertain
from resource_controller.storage import Store


class CommandTests(unittest.TestCase):
    def test_timeout_and_bad_output_preserve_pending_and_never_kill_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / 'child.py'
            stub.write_text('import time,sys,json\ntime.sleep(float(sys.argv[1]))\nprint(json.dumps({"ok": True}))\n')
            store = Store(root / 'state')
            command = Command(store)
            command.start([sys.executable, str(stub), '0'], os.environ.copy(), root, {'before': []})
            self.assertEqual(command.wait(5), {'ok': True})
            # Only the caller can clear after post-command reconciliation.
            self.assertIsNotNone(store.read('pending.json'))
            store.remove('pending.json')
            command.start([sys.executable, str(stub), '.4'], os.environ.copy(), root, {'before': []})
            with self.assertRaises(Uncertain):
                command.wait(.01)
            self.assertIsNone(command.process.poll())
            self.assertTrue(store.read('pending.json')['uncertain'])
            with self.assertRaises(Uncertain):
                command.start([sys.executable, str(stub), '0'], os.environ.copy(), root, {})
            command.process.wait(timeout=5)
            command.drain()
            self.assertIsNotNone(store.read('pending.json'))
            store.remove('pending.json')
            stub.write_text('print("x" * 100000)\n')
            command.start([sys.executable, str(stub), '0'], os.environ.copy(), root, {})
            with self.assertRaises(Uncertain):
                command.wait(5)
            command.process.wait(timeout=5)
            command.drain()
            self.assertLessEqual(len(command.output), 65536)
            for program in ('print("not JSON")\n', 'import sys\nsys.exit(3)\n'):
                store.remove('pending.json')
                stub.write_text(program)
                command.start([sys.executable, str(stub)], os.environ.copy(), root, {})
                with self.assertRaises(Uncertain):
                    command.wait(5)
                self.assertIsNotNone(store.read('pending.json'))
