import json
import os
from pathlib import Path
import tempfile
import unittest

from resource_controller.storage import Store, Singleton


class StorageTests(unittest.TestCase):
    def test_private_atomic_journal_survives_restart_and_lock_excludes_peers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'
            store = Store(root)
            store.write('pending.json', {'board': 'default', 'command': 'dispatch'})
            self.assertEqual(Store(root).read('pending.json')['board'], 'default')
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / 'pending.json').stat().st_mode & 0o777, 0o600)
            store.write('status.json', {'reason': 'resource:load'})
            self.assertEqual(json.loads((root / 'status.json').read_text())['reason'], 'resource:load')
            with Singleton(root / '.dispatcher.lock') as lock:
                self.assertFalse(os.get_inheritable(lock.fd))
                with self.assertRaises(BlockingIOError):
                    with Singleton(root / '.dispatcher.lock'):
                        self.fail('duplicate authority')
            with Singleton(root / '.dispatcher.lock'):
                pass
            store.log('x' * 2000)
            for _ in range(100):
                store.log('y' * 2000)
            self.assertLessEqual(sum(p.stat().st_size for p in root.glob('events.log*')), 3 * 65536)
            outside = Path(tmp) / 'outside'
            outside.write_text('unchanged')
            (root / 'unsafe.json').symlink_to(outside)
            with self.assertRaises(ValueError):
                store.write('unsafe.json', {})
            self.assertEqual(outside.read_text(), 'unchanged')
