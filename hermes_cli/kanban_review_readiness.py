"""Board-local writer readiness, issued by actual supported runtime entry points.

These receipts attest local runtime capability, not remote provider identity or
protection from arbitrary code executed as the account owner. A new challenge
supersedes old readiness without changing any enrolled budget or hold.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import platform
import sys
import types
import uuid
from functools import lru_cache
from pathlib import Path

import psutil

from hermes_cli.kanban_db_connect import write_txn

PROTOCOL = 1
SURFACES = frozenset({'cli', 'gateway', 'dashboard'})
_MODULES = ('kanban_review_state', 'kanban_review_guards', 'kanban_review_cohort',
            'kanban_review_operator', 'kanban_review_readiness', 'kanban_review_legacy',
            'kanban_review_worker', 'kanban_review_transport', 'kanban_worker_launch')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS review_readiness (
        id TEXT PRIMARY KEY, board_id TEXT NOT NULL, current INTEGER NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS review_writer_receipts (
        id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, surface TEXT NOT NULL,
        receipt TEXT NOT NULL, UNIQUE(challenge_id,surface))''')


def _code_value(value):
    if isinstance(value, types.CodeType):
        return {key: _code_value(getattr(value, key)) for key in (
            'co_code', 'co_consts', 'co_names', 'co_varnames', 'co_freevars',
            'co_cellvars', 'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_flags')}
    if isinstance(value, bytes):
        return {'bytes': value.hex()}
    if isinstance(value, (tuple, list)):
        return [_code_value(v) for v in value]
    if isinstance(value, frozenset):
        return sorted((_code_value(v) for v in value), key=repr)
    if value is None or type(value) in (str, int, float, bool):
        return value
    return repr(value)


@lru_cache(maxsize=1)
def runtime_digest():
    # Hash loaded functions, not the checkout HEAD or files that a running old
    # interpreter may no longer be executing. Python ABI differences fail closed.
    code = {}
    for name in _MODULES:
        module = importlib.import_module('hermes_cli.' + name)
        code[name] = {key: _code_value(value.__code__) for key, value in vars(module).items()
                      if inspect.isfunction(value) and value.__module__ == module.__name__}
    # Non-workflow dispatcher helpers (resource sampling, notifications, etc.)
    # aren't this protocol. Include the actual shared lifecycle entry points.
    kb = importlib.import_module('hermes_cli.kanban_db')
    code['lifecycle'] = {name:_code_value(getattr(kb, name).__code__) for name in (
        'claim_task', 'claim_review_task', 'complete_task', 'request_review', 'request_changes')}
    return hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()


def _board(conn):
    row = conn.execute('SELECT board_id,schema_version FROM workflow_board WHERE singleton=1').fetchone()
    if not row or row['schema_version'] != PROTOCOL:
        raise ValueError('readiness requires a supported board schema')
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('readiness requires a persistent board')
    return row['board_id'], str(Path(path).resolve())


def _operator():
    from agent.delegation_context import is_delegated_child_context
    if os.environ.get('HERMES_KANBAN_TASK') or is_delegated_child_context():
        raise ValueError('workers cannot authorize writer readiness')


def begin(conn):
    _operator()
    with write_txn(conn):
        board_id, _ = _board(conn)
        challenge = str(uuid.uuid4())
        conn.execute('UPDATE review_readiness SET current=0')
        conn.execute('INSERT INTO review_readiness VALUES(?,?,1)', (challenge, board_id))
    return challenge


def issue(conn, challenge, surface):
    _operator()
    if surface not in SURFACES:
        raise ValueError('unknown readiness writer')
    with write_txn(conn):
        board_id, board_path = _board(conn)
        row = conn.execute('SELECT * FROM review_readiness WHERE id=? AND current=1', (challenge,)).fetchone()
        if not row or row['board_id'] != board_id:
            raise ValueError('stale or foreign readiness challenge')
        proc = psutil.Process()
        receipt = dict(id=str(uuid.uuid4()), challenge_id=challenge, board_id=board_id,
                       board_path=board_path, protocol=PROTOCOL, runtime_digest=runtime_digest(),
                       surface=surface, pid=proc.pid, started_at=proc.create_time(),
                       host=platform.node(), executable=str(Path(sys.executable).resolve()),
                       source=str(Path(__file__).resolve().parent), python=sys.implementation.cache_tag)
        conn.execute('''INSERT INTO review_writer_receipts VALUES(?,?,?,?)
            ON CONFLICT(challenge_id,surface) DO UPDATE SET id=excluded.id,receipt=excluded.receipt''',
            (receipt['id'], challenge, surface, json.dumps(receipt, sort_keys=True)))
    return receipt


def verify(conn, selection):
    """Read back every selected receipt under the enrollment's write transaction."""
    fields = SURFACES | {'challenge_id'}
    if not isinstance(selection, dict) or set(selection) != fields:
        raise ValueError('verified three-writer readiness selection required')
    if any(not isinstance(value, str) for value in selection.values()):
        raise ValueError('readiness receipt identifiers required, not declared versions')
    board_id, board_path = _board(conn)
    challenge = conn.execute('SELECT * FROM review_readiness WHERE id=? AND current=1',
                             (selection['challenge_id'],)).fetchone()
    if not challenge or challenge['board_id'] != board_id:
        raise ValueError('stale or foreign readiness challenge')
    receipts = {}
    for surface in sorted(SURFACES):
        row = conn.execute('SELECT receipt FROM review_writer_receipts WHERE id=? AND challenge_id=? AND surface=?',
                           (selection[surface], selection['challenge_id'], surface)).fetchone()
        if not row:
            raise ValueError('missing or superseded readiness writer receipt')
        receipt = json.loads(row[0])
        expected = {'board_id':board_id, 'board_path':board_path, 'protocol':PROTOCOL,
                    'runtime_digest':runtime_digest(), 'host':platform.node(),
                    'python':sys.implementation.cache_tag, 'surface':surface}
        if any(receipt.get(k) != v for k,v in expected.items()):
            raise ValueError('unknown, foreign or mixed-version readiness writer')
        # CLI is a finite process: its loaded-code fingerprint is rechecked by
        # this CLI. Serving writers must still be the exact observed processes.
        if surface != 'cli':
            try:
                proc = psutil.Process(receipt['pid'])
                alive = proc.create_time() == receipt['started_at'] and proc.status() != psutil.STATUS_ZOMBIE
            except (psutil.Error, KeyError):
                alive = False
            if not alive:
                raise ValueError('stale readiness writer process; repeat preflight')
        receipts[surface] = receipt
    return {'selection':selection, 'runtime_digest':runtime_digest(), 'writers':receipts}


def compatible(attempt):
    value = attempt['compatibility']
    return isinstance(value, dict) and value.get('runtime_digest') == runtime_digest()


def gateway_issue(home, params):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_db as kb
    if (not isinstance(params, dict) or set(params) - {'challenge_id','board'}
            or not isinstance(params.get('challenge_id'), str)):
        raise ValueError('readiness requires challenge_id and optional board slug')
    token = set_hermes_home_override(home)
    try:
        board = kb._normalize_board_slug(params.get('board'))
        with connect_closing(board=board) as conn:
            return issue(conn, params['challenge_id'], 'gateway')
    finally:
        reset_hermes_home_override(token)
