import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from resource_controller.runtime import Runtime, Settings, scan_processes
from resource_controller.storage import Store, Singleton

SOURCE = Path(__file__).resolve().parents[3]


class RuntimeTests(unittest.TestCase):
    def test_live_config_isolated_home_a_b_a(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtimes = []
            for name, enabled in [('a', 'false'), ('b', 'true')]:
                home = root / name
                home.mkdir()
                (home / 'config.yaml').write_text('kanban:\n  auto_decompose: ' + enabled + '\n')
                settings = Settings(SOURCE, Path(sys.executable), home, root, root / (name + '-state'))
                runtimes.append(Runtime(settings, Store(settings.state)))
            a, b = runtimes
            self.assertFalse(a.config()['auto_decompose'])
            self.assertTrue(b.config()['auto_decompose'])
            self.assertFalse(a.config()['auto_decompose'])
            (a.settings.home / 'config.yaml').write_text('kanban:\n  auto_decompose: true\n')
            self.assertTrue(a.config()['auto_decompose'])

    def test_real_cli_isolated_dispatch_board_cap_and_read_only_enumeration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            home.mkdir()
            (home / 'config.yaml').write_text('kanban:\n  dispatch_in_gateway: false\n  max_in_progress: 2\n  max_in_progress_per_profile: 1\n  dispatch_stale_timeout_seconds: 0\n  auto_decompose: false\n')
            settings = Settings(SOURCE, Path(sys.executable), home, root, root / 'state')
            runtime = Runtime(settings, Store(settings.state))
            # The real supported dispatcher uses its HERMES_BIN extension point;
            # only the worker is a harmless, finite stub. No Hermes monkeypatch.
            stub = root / 'hermes-stub'
            stub.write_text('#!' + sys.executable + '\nimport os,time\nfrom pathlib import Path\np=Path(os.environ["HERMES_KANBAN_HOME"])\n(p/"worker.pid").write_text(str(os.getpid()))\ndeadline=time.monotonic()+60\nwhile p.exists() and not (p/"release").exists() and time.monotonic()<deadline: time.sleep(.05)\n')
            stub.chmod(0o700)
            runtime.env['HERMES_BIN'] = str(stub)
            def cli(*args):
                done = subprocess.run(runtime.argv(*args), env=runtime.env, cwd=home,
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
                return json.loads(done.stdout)
            tasks = [cli('kanban', 'create', 'harmless canary', '--assignee', 'default', '--json')['id'] for _ in range(2)]
            before = (home / 'kanban.db').read_bytes()
            self.assertEqual(runtime.boards(), ['default'])
            self.assertEqual((home / 'kanban.db').read_bytes(), before)
            try:
                (home / 'kanban').mkdir(exist_ok=True)
                with Singleton(home / 'kanban' / '.dispatcher.lock'):
                    result = runtime.execute('default', 'dispatch', None, {'config': {'failure_limit': 2}})
                    self.assertEqual(len(result['spawned']), 1)
                    self.assertIsNotNone(runtime.store.read('launched.json'))
                    runtime.store.remove('pending.json')
                    deadline = time.monotonic() + 10
                    while not (home / 'worker.pid').exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue((home / 'worker.pid').exists())
                    pid = int((home / 'worker.pid').read_text())
                    self.assertTrue(any(p.pid == pid for p in scan_processes()))
                    inventory = runtime.snapshot()
                    self.assertTrue(any(w['pid'] == pid for w in inventory['workers']), inventory['holds'])
                    self.assertFalse(inventory['estop'])
                    (home / 'ESTOP').write_text('manual operator pause')
                    self.assertTrue(runtime.snapshot()['estop'])
                    self.assertEqual((home / 'ESTOP').read_text(), 'manual operator pause')
                    # Supported dispatch doesn't take the gateway singleton lock.
                    # --max1 is board concurrency, not a burst-size flag.
                    second = runtime.execute('default', 'dispatch', None, {'config': {'failure_limit': 2}})
                    self.assertEqual(second['spawned'], [])
                    runtime.store.remove('pending.json')
                    self.assertEqual(len(cli('kanban', 'list', '--status', 'running', '--json')), 1)
                    complete = subprocess.run(runtime.argv('kanban', 'complete', result['spawned'][0]['task_id'], '--summary', 'synthetic complete'),
                                              env=runtime.env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(complete.returncode, 0, complete.stderr)
                    terminal = runtime.snapshot()
                    self.assertTrue(any(w['pid'] == pid for w in terminal['workers']), {'runs': terminal['runs'], 'holds': terminal['holds'], 'pid': pid})
                    self.assertFalse(runtime.tracker.drained())
            finally:
                (home / 'release').touch()
                deadline = time.monotonic() + 10
                if (home / 'worker.pid').exists():
                    pid = int((home / 'worker.pid').read_text())
                    while any(p.pid == pid for p in scan_processes()) and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(any(p.pid == pid for p in scan_processes()))
            self.assertEqual(set(tasks), {t['id'] for t in cli('kanban', 'list', '--json')})
            self.assertEqual(runtime.config()['auto_decompose'], False)
            print('CANARY: real CLI, temp roots, one harmless worker; --max1 second dispatch spawned zero; shared lock held; worker exited naturally.')
