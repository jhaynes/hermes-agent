import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from resource_controller.lifecycle import DrainTracker
from resource_controller.storage import Store

APP = Path(__file__).resolve().parents[1] / 'controller.py'
SOURCE = Path(__file__).resolve().parents[3]


def until(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.1)
    return False


class ApplicationTests(unittest.TestCase):
    def test_supervision_error_holds_instead_of_exiting_worker_coalition(self):
        from types import SimpleNamespace
        from controller import supervise
        from resource_controller.engine import Controller
        with tempfile.TemporaryDirectory() as tmp:
            def denied():
                raise PermissionError('cannot prove child liveness')
            store = Store(Path(tmp) / 'state')
            runtime = SimpleNamespace(drain=denied)
            controller = Controller(store, runtime, active=True)
            self.assertFalse(supervise(runtime, controller))
            self.assertEqual(store.read('status.json')['reason'], 'identity:supervision-unavailable')

    def test_acknowledgement_requires_quiescence_and_preserves_manual_hold(self):
        from types import SimpleNamespace
        from controller import acknowledge
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state')
            reader = Store(store.root / 'reader')
            runtime = SimpleNamespace(store=store, reader=SimpleNamespace(store=reader),
                                      tracker=DrainTracker(store),
                                      snapshot=lambda: {'workers': [], 'holds': []})
            store.write('hold.json', {'reason': 'operator'})
            store.write('pending.json', {'pid': None, 'created': None})
            with self.assertRaises(ValueError):
                acknowledge(runtime, 'inspected')
            child = subprocess.Popen([sys.executable, '--version'], stdout=subprocess.DEVNULL)
            import psutil
            created = psutil.Process(child.pid).create_time()
            child.wait(timeout=10)
            store.write('pending.json', {'pid': child.pid, 'created': created})
            acknowledge(runtime, 'canonical and process state inspected')
            self.assertIsNone(store.read('pending.json'))
            self.assertIsNotNone(store.read('hold.json'))
            self.assertEqual(store.read('acknowledgement.json')['reason'], 'canonical and process state inspected')

    def test_operator_resume_refuses_uncertainty_and_always_resets_dwell(self):
        from controller import resume_admission
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state')
            Store(store.root / 'reader')
            store.write('hold.json', {'reason': 'manual'})
            store.write('pending.json', {'pid': None})
            with self.assertRaises(ValueError):
                resume_admission(store)
            self.assertIsNotNone(store.read('hold.json'))
            store.remove('pending.json')
            resume_admission(store)
            self.assertIsNone(store.read('hold.json'))
            self.assertIsNotNone(store.read('reset.json'))

    def test_observation_never_claims_dispatch_lock_and_stop_drains_without_killing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            home.mkdir()
            (home / 'config.yaml').write_text('kanban:\n  dispatch_in_gateway: false\n  max_in_progress: 2\n  max_in_progress_per_profile: 1\n  dispatch_stale_timeout_seconds: 0\n  auto_decompose: false\n')
            state = root / 'state'
            config = root / 'controller.json'
            config.write_text(json.dumps({'source': str(SOURCE), 'python': sys.executable,
                                          'home': str(home), 'user_home': str(root), 'state': str(state)}))
            config.chmod(0o600)
            env = {**os.environ, 'HOME': str(root), 'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home)}
            init = subprocess.run([sys.executable, str(SOURCE / 'hermes'), 'kanban', 'init'], env=env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(init.returncode, 0, init.stderr)
            def app(verb):
                return subprocess.run([sys.executable, str(APP), '--config', str(config), verb],
                                      env=env, capture_output=True, text=True, timeout=20)
            observe = app('observe')
            self.assertEqual(observe.returncode, 0, observe.stderr)
            self.assertFalse(json.loads(observe.stdout)['authority'])
            self.assertFalse((home / 'kanban' / '.dispatcher.lock').exists())
            child_script = root / 'synthetic.py'
            child_script.write_text('import sys,time\nfrom pathlib import Path\nwhile not Path(sys.argv[1]).exists(): time.sleep(.05)\n')
            release = root / 'release'
            child = subprocess.Popen([sys.executable, str(child_script), str(release)], start_new_session=True)
            DrainTracker(Store(state)).include([child.pid])
            service = subprocess.Popen([sys.executable, str(APP), '--config', str(config), 'run'], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(until(lambda: (home / 'kanban' / '.dispatcher.lock').exists()))
                Store(state).write('exit.json', {'requested': False})
                (state / 'exit.json').write_text('malformed controller state')
                self.assertTrue(until(lambda: Store(state).read('status.json').get('reason') == 'compatibility:state-unavailable'))
                self.assertIsNone(service.poll())
                self.assertEqual(app('stop').returncode, 0)
                self.assertTrue(until(lambda: Store(state).read('status.json').get('reason') == 'manual:draining'))
                self.assertIsNone(service.poll())
                self.assertIsNone(child.poll())
                release.touch()
                child.wait(timeout=10)
                self.assertEqual(service.wait(timeout=20), 0)
                self.assertEqual(Store(state).read('status.json')['reason'], 'manual:drained')
            finally:
                release.touch()
                child.wait(timeout=10)
                Store(state).write('exit.json', {'requested': True})
                service.wait(timeout=30)
