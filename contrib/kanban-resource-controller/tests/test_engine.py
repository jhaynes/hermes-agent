from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from resource_controller.engine import Controller, choose
from resource_controller.policy import Sample
from resource_controller.storage import Store


def healthy(t):
    return Sample(t, 10, 1, 8 * 2**30, 1, 0, 0)


def snapshot():
    return {'holds': [], 'workers': [], 'runs': [], 'estop': False,
            'config': {'auto_decompose': True, 'default_assignee': 'default', 'failure_limit': 2},
            'boards': {'a': [{'id': 't_12345678', 'status': 'ready', 'assignee': 'builder'}],
                       'b': [{'id': 't_abcdef12', 'status': 'triage', 'assignee': 'builder'}]}}


class Backend:
    def __init__(self):
        self.value = snapshot()
        self.calls = []
        self.reads = 0

    def snapshot(self):
        self.reads += 1
        return deepcopy(self.value)

    def execute(self, board, verb, task, before):
        self.calls.append((board, verb, task))
        return {'spawned': []} if verb == 'dispatch' else {'task_id': task, 'ok': True}

    def drain(self):
        pass


class EngineTests(unittest.TestCase):
    def test_one_action_per_window_round_robin_ready_before_triage_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Backend()
            backend.value['boards']['a'].append({'id': 't_deadbeef', 'status': 'triage', 'assignee': 'builder'})
            store = Store(Path(tmp) / 'state')
            controller = Controller(store, backend, active=True)
            for t in range(0, 151, 30):
                status = controller.tick(healthy(t), lambda: healthy(t + .01))
            self.assertEqual(backend.calls, [('a', 'dispatch', None)])
            self.assertGreaterEqual(backend.reads, 8)  # pre and post reconciliation are real calls
            for t in range(180, 331, 30):
                controller.tick(healthy(t), lambda: healthy(t + .01))
            self.assertEqual(backend.calls[-1], ('b', 'decompose', 't_abcdef12'))
            self.assertEqual(len(backend.calls), 2)
            backend.value['config']['auto_decompose'] = False
            for t in range(360, 511, 30):
                status = controller.tick(healthy(t), lambda: Sample(t + .01, 10, 11, 8 * 2**30, 1, 0, 0))
            self.assertEqual(status['reason'], 'resource:load')
            self.assertEqual(len(backend.calls), 2)
            observation = Controller(store, backend, active=False)
            for t in range(360, 511, 30):
                observation.tick(healthy(t))
            self.assertEqual(len(backend.calls), 2)
            self.assertFalse(store.read('status.json')['authority'])

    def test_holds_defer_every_mutation_and_uncertain_results_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Backend()
            store = Store(Path(tmp) / 'state')
            controller = Controller(store, backend, active=True)
            backend.value['estop'] = True
            for t in range(0, 181, 30):
                self.assertEqual(controller.tick(healthy(t))['reason'], 'ESTOP')
            self.assertEqual(backend.calls, [])
            backend.value['estop'] = False
            backend.value['workers'] = [{'board': 'other', 'profile': 'builder'}, {'board': 'other', 'profile': 'else'}]
            self.assertEqual(controller.tick(healthy(210))['reason'], 'capacity:host')
            backend.value['workers'] = [{'board': 'other', 'profile': 'builder'}]
            self.assertEqual(choose(backend.value, 'a')[2], 'capacity:profile')
            backend.value['workers'][0]['board'] = 'a'
            self.assertEqual(choose(backend.value, 'a')[2], 'capacity:board')
            backend.value['workers'] = []
            store.write('hold.json', {'reason': 'drain'})
            self.assertEqual(controller.tick(healthy(240))['reason'], 'manual:hold')
            store.remove('hold.json')
            backend.execute = lambda *args: {'malformed': True}
            for t in range(270, 421, 30):
                result = controller.tick(healthy(t), lambda: healthy(t + .01))
            self.assertEqual(result['reason'], 'uncertain-outcome')
            self.assertIsNotNone(store.read('pending.json'))
            restarted = Controller(store, backend, active=True)
            self.assertEqual(restarted.tick(healthy(450))['reason'], 'uncertain-outcome')
            def broken_snapshot():
                raise ValueError('cannot read canonical state')
            backend.snapshot = broken_snapshot
            self.assertEqual(restarted.tick(healthy(480))['reason'], 'uncertain-outcome')
            store.remove('pending.json')
            from resource_controller.command import Uncertain
            def uncertain_reader():
                raise Uncertain('persistent reader journal')
            backend.snapshot = uncertain_reader
            self.assertEqual(restarted.tick(healthy(510))['reason'], 'uncertain-outcome')
