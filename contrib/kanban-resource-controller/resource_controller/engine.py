"""One admission decision; external side effects stay behind the CLI adapter."""
import time

from .policy import Gate
from .command import Uncertain


def hold_reason(snapshot):
    if snapshot['estop']:
        return 'ESTOP'
    if snapshot['holds']:
        return snapshot['holds'][0]
    if len(snapshot['workers']) >= 2:
        return 'capacity:host'
    return None


def choose(snapshot, board):
    workers = snapshot['workers']
    if any(w['board'] == board for w in workers):
        return None, None, 'capacity:board'
    tasks = snapshot['boards'][board]
    candidates = [t for t in tasks if t['status'] in ('ready', 'todo', 'review')]
    profiles = {w['profile'] for w in workers}
    default = snapshot['config'].get('default_assignee') or None
    # --max 1 can select ANY eligible task after promotion. Refuse a board if
    # that could select a profile whose completed worker is still alive.
    if candidates:
        if any((t['assignee'] or default) in profiles for t in candidates):
            return None, None, 'capacity:profile'
        return 'dispatch', None, None
    triage = [t for t in tasks if t['status'] == 'triage']
    if triage and snapshot['config']['auto_decompose']:
        return 'decompose', triage[0]['id'], None
    return None, None, 'idle'


def validate_result(result, verb, task, after):
    if not isinstance(result, dict):
        raise ValueError('CLI result is not an object')
    if verb == 'decompose':
        if result.get('ok') is not True or result.get('task_id') != task:
            raise ValueError('decompose outcome not confirmed')
        return
    spawned = result.get('spawned')
    if not isinstance(spawned, list) or len(spawned) > 1:
        raise ValueError('dispatch violated --max 1 contract')
    for item in spawned:
        if not isinstance(item, dict) or not any(r['task_id'] == item.get('task_id') and r['pid'] and r['stamp']
                                               for r in after['runs']):
            raise ValueError('spawn not corroborated by canonical run and fingerprint')


class Controller:
    def __init__(self, store, backend, *, active=False):
        self.store, self.backend, self.active = store, backend, active
        self.gate = Gate()  # Restart never restores a healthy dwell.
        self.last_board = None

    def status(self, reason, snapshot=None, **extra):
        result = {'at': time.time(), 'reason': reason, 'authority': self.active,
                  'workers': (snapshot or {}).get('workers', []),
                  'holds': (snapshot or {}).get('holds', []), **extra}
        self.store.write('status.json', result)
        self.store.log(reason)
        return result

    def blocked(self, reason, snapshot=None):
        self.gate.reset()
        return self.status(reason, snapshot)

    def tick(self, sample, fresh_sample=None):
        self.backend.drain()
        pending = self.store.read('pending.json')
        try:
            snapshot = self.backend.snapshot()
        except Exception as exc:
            if pending is not None or isinstance(exc, Uncertain):
                self.store.write('reconciliation.json', {'at': time.time(), 'after': None})
                return self.blocked('uncertain-outcome')
            return self.blocked('compatibility:inventory-unavailable', {'holds': [type(exc).__name__]})
        if pending is not None:
            self.store.write('reconciliation.json', {'at': time.time(), 'after': snapshot})
            return self.blocked('uncertain-outcome', snapshot)
        if self.store.read('hold.json') is not None:
            return self.blocked('manual:hold', snapshot)
        hold = hold_reason(snapshot)
        if hold:
            return self.blocked(hold, snapshot)
        reason = self.gate.observe(sample)
        if reason != 'eligible':
            return self.status(reason, snapshot)
        boards = sorted(snapshot['boards'])
        if not boards:
            return self.status('idle', snapshot)
        later = [b for b in boards if self.last_board is None or b > self.last_board]
        board = (later or boards)[0]
        self.last_board = board  # Advance even if this board cannot use its turn.
        verb, task, reason = choose(snapshot, board)
        if not verb:
            return self.status(reason, snapshot, board=board)
        if not self.active:
            return self.status('observation:would-' + verb, snapshot, board=board)
        # A manual start / ESTOP / live toggle may have changed during telemetry.
        before = self.backend.snapshot()
        hold = hold_reason(before)
        if hold or self.store.read('hold.json') is not None or self.store.read('reset.json') is not None:
            return self.blocked(hold or 'manual:hold', before)
        if board not in before['boards'] or choose(before, board)[:2] != (verb, task):
            return self.blocked('compatibility:pre-command-change', before)
        reason = self.gate.observe(fresh_sample() if fresh_sample is not None else None)
        if reason != 'eligible':
            return self.status(reason, before)
        self.gate.reset()  # Every possible side effect, even a no-op, consumes the window.
        try:
            result = self.backend.execute(board, verb, task, before)
            after = self.backend.snapshot()
            self.store.write('reconciliation.json', {'before': before, 'after': after, 'at': time.time()})
            validate_result(result, verb, task, after)
            if after['holds']:
                raise ValueError('post-command identity/contract hold')
        except Exception as exc:
            pending = self.store.read('pending.json') or {'board': board, 'verb': verb, 'task': task, 'before': before}
            pending.update(uncertain=True, error=type(exc).__name__)
            self.store.write('pending.json', pending)
            try:
                after = self.backend.snapshot()
                self.store.write('reconciliation.json', {'before': before, 'after': after, 'at': time.time()})
            except Exception:
                self.store.write('reconciliation.json', {'before': before, 'after': None, 'at': time.time()})
            return self.status('uncertain-outcome', before)
        self.store.remove('pending.json')
        return self.status('cooldown:' + verb, after, board=board)
