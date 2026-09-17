"""Persistent bounded review lineage. Legacy cards are not implicitly enrolled."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid

from hermes_cli.kanban_db_connect import write_txn

POLICY = {'version': 1, 'rounds': 3, 'recovery': 2, 'active_seconds': 7200,
          'postmortem_runs': 2, 'postmortem_seconds': 600}
REQUIRED_LANES = frozenset({'tests', 'quality', 'architecture', 'style',
                            'breaker_a', 'breaker_b', 'breaker_c', 'scope'})


def initialize(conn):
    with write_txn(conn, allow_nested=True):
        conn.execute('''CREATE TABLE IF NOT EXISTS review_attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, board_id TEXT NOT NULL,
            spec_digest TEXT NOT NULL, policy_digest TEXT NOT NULL, policy TEXT NOT NULL,
            base_sha TEXT NOT NULL, target_sha TEXT NOT NULL, implementer_maker TEXT NOT NULL,
            roster TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
            completed_rounds INTEGER NOT NULL, recovery_used INTEGER NOT NULL,
            active_seconds REAL NOT NULL, decision TEXT NOT NULL, compatibility TEXT NOT NULL,
            hold_reason TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS review_actions (
            id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, task_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL, category TEXT NOT NULL, recovery INTEGER NOT NULL,
            state TEXT NOT NULL, run_id INTEGER UNIQUE, deadline REAL,
            UNIQUE(attempt_id,ordinal,category,recovery))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS review_clock (
            attempt_id TEXT PRIMARY KEY, boot_id TEXT NOT NULL,
            monotonic_at REAL NOT NULL, wall_at REAL NOT NULL)''')
        from hermes_cli.kanban_review_cohort import initialize as initialize_cohort
        initialize_cohort(conn)


def install_guard(conn):
    # A board with no enrollment must remain usable by legacy writers. SQLite
    # resolves UDFs at statement preparation, even when a trigger WHEN is false.
    conn.execute('''CREATE TRIGGER IF NOT EXISTS review_task_guard
        BEFORE UPDATE OF status ON tasks
        WHEN EXISTS(SELECT 1 FROM review_attempts WHERE task_id=OLD.id)
          OR EXISTS(SELECT 1 FROM review_members WHERE task_id=OLD.id)
        BEGIN
            SELECT CASE WHEN workflow_transition_allowed(OLD.id, NEW.status)=0
                THEN RAISE(ABORT, 'managed workflow transition refused') END;
        END''')


def bind_connection(conn):
    # The persistent trigger calls a runtime capability absent on legacy writers.
    # This is a downgrade guard, not a sandbox against arbitrary SQL by the owner.
    conn.create_function('workflow_transition_allowed', 2,
                         lambda task_id, status: transition_allowed(conn, task_id, status))


def transition_allowed(conn, task_id, status):
    attempt = get_attempt(conn, task_id)
    if attempt is None:
        return True
    if attempt['policy']['version'] != 1:
        return False
    if status == 'done':
        member = conn.execute('SELECT state FROM review_members WHERE task_id=?', (task_id,)).fetchone()
        return member[0] in {'received_valid','invalid','failed'} if member else attempt['state'] == 'approved'
    if status == 'running':
        return claim_allowed(conn, task_id)
    return status in {'blocked', 'review', 'todo', 'triage', 'ready'}


def claim_allowed(conn, task_id):
    # Managed dispatch requires an explicit workflow action reservation. Until
    # one is admitted, even a manually changed ready column grants no authority.
    from hermes_cli.kanban_postmortem import claim_allowed as diagnostic_allowed
    if not diagnostic_allowed(conn, task_id):
        return False
    attempt = get_attempt(conn, task_id)
    if attempt is None:
        return True
    if attempt['state'] not in {'preflight', 'reviewing', 'repair'}:
        return False
    return conn.execute("SELECT 1 FROM review_actions WHERE task_id=? AND state='reserved'", (task_id,)).fetchone() is not None


def reserve_action(conn, task_id, *, category, expected_version, recovery=False):
    """Spend a primary action or shared recovery before any process can launch."""
    with write_txn(conn):
        attempt = get_attempt(conn, task_id)
        if not attempt or attempt['version'] != expected_version:
            raise ValueError('attempt CAS lost')
        if attempt['state'] in {'held', 'approved', 'cancelled'}:
            return None
        if category not in {'preflight', 'repair'} or category != attempt['state']:
            raise ValueError('action does not match attempt phase')
        live = conn.execute("SELECT 1 FROM review_actions WHERE attempt_id=? AND state IN ('reserved','running')", (attempt['id'],)).fetchone()
        if live:
            raise ValueError('action already reserved or running')
        previous = conn.execute('SELECT state FROM review_actions WHERE attempt_id=? AND ordinal=? AND category=? ORDER BY recovery DESC LIMIT 1',
                                (attempt['id'], attempt['completed_rounds']+1, category)).fetchone()
        if bool(previous) != bool(recovery) or (previous and previous[0] != 'failed'):
            raise ValueError('only a failed action may use recovery; primary cannot repeat')
        policy = attempt['policy']
        exhausted = ('review_exhausted' if attempt['completed_rounds'] >= policy['rounds'] else
                     'active_time_exhausted' if attempt['active_seconds'] >= policy['active_seconds'] else
                     'recovery_exhausted' if recovery and attempt['recovery_used'] >= policy['recovery'] else None)
        if exhausted:
            hold(conn, attempt, exhausted)
            return None
        used = attempt['recovery_used'] + int(recovery)
        action_id = str(uuid.uuid4())
        conn.execute('INSERT INTO review_actions(id,attempt_id,task_id,ordinal,category,recovery,state) VALUES(?,?,?,?,?,?,?)',
                     (action_id, attempt['id'], task_id, attempt['completed_rounds']+1, category, used if recovery else 0, 'reserved'))
        conn.execute('UPDATE review_attempts SET recovery_used=?,version=version+1 WHERE id=?', (used, attempt['id']))
        from hermes_cli.kanban_db import _append_event
        _append_event(conn, task_id, 'review_action_reserved', {'attempt_id':attempt['id'],'action_id':action_id,'category':category,'recovery':used})
        return action_id


def hold(conn, attempt, reason):
    """Caller owns the transaction. The state gate is independent of task column."""
    conn.execute("UPDATE review_attempts SET state='held',hold_reason=?,version=version+1 WHERE id=?", (reason, attempt['id']))
    conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (attempt['task_id'],))
    from hermes_cli.kanban_db import _append_event
    _append_event(conn, attempt['task_id'], reason, {'attempt_id':attempt['id']})


def claimed(conn, task_id, run_id):
    from hermes_cli.kanban_postmortem import claimed as diagnostic_claimed
    diagnostic_claimed(conn, task_id, run_id)
    attempt = get_attempt(conn, task_id)
    if attempt is None:
        return
    settle_clock(conn, attempt)
    attempt = get_attempt(conn, task_id)
    row = conn.execute('SELECT max_runtime_seconds FROM tasks WHERE id=?', (task_id,)).fetchone()
    remaining = max(1, math.floor(attempt['policy']['active_seconds'] - attempt['active_seconds']))
    cap = min(remaining, row[0]) if row[0] and row[0] > 0 else remaining
    conn.execute("UPDATE review_actions SET state='running',run_id=?,deadline=? WHERE task_id=? AND state='reserved'", (run_id,time.time()+cap,task_id))
    conn.execute('UPDATE tasks SET max_runtime_seconds=? WHERE id=?', (cap,task_id))
    conn.execute('UPDATE task_runs SET max_runtime_seconds=? WHERE id=?', (cap,run_id))


def released(conn, task_id, run_id, outcome):
    from hermes_cli.kanban_postmortem import released as diagnostic_released
    diagnostic_released(conn, task_id, run_id, outcome)
    attempt = get_attempt(conn, task_id)
    if attempt is not None:
        settle_clock(conn, attempt)
    conn.execute("UPDATE review_actions SET state=? WHERE task_id=? AND run_id=? AND state='running'",
                 ('complete' if outcome in {'completed','review_requested'} else 'failed', task_id,run_id))


def settle_clock(conn, attempt):
    """Charge union-active time once between authoritative lifecycle transitions."""
    import psutil
    boot = str(psutil.boot_time())
    mono, wall = time.monotonic(), time.time()
    clock = conn.execute('SELECT * FROM review_clock WHERE attempt_id=?', (attempt['id'],)).fetchone()
    running = conn.execute("SELECT 1 FROM review_actions WHERE attempt_id=? AND state='running' LIMIT 1", (attempt['id'],)).fetchone()
    if clock and running:
        if boot != clock['boot_id'] or mono < clock['monotonic_at']:
            hold(conn, attempt, 'clock_reconciliation_required')
            return
        elapsed = mono - clock['monotonic_at']
        conn.execute('UPDATE review_attempts SET active_seconds=active_seconds+? WHERE id=?', (elapsed, attempt['id']))
    conn.execute('''INSERT INTO review_clock VALUES(?,?,?,?)
        ON CONFLICT(attempt_id) DO UPDATE SET boot_id=excluded.boot_id,
        monotonic_at=excluded.monotonic_at,wall_at=excluded.wall_at''', (attempt['id'],boot,mono,wall))


def get_attempt(conn, task_id):
    row = conn.execute('SELECT * FROM review_attempts WHERE task_id=? OR id IN (SELECT attempt_id FROM review_members WHERE task_id=?)', (task_id,task_id)).fetchone()
    if not row:
        return None
    out = dict(row)
    for name in ('roster', 'policy', 'compatibility'):
        out[name] = json.loads(out[name])
    return out


def enroll_review(conn, task_id, *, expected_status, expected_run_id, board_id,
                  spec_digest, base_sha, target_sha, implementer_maker, roster,
                  consumed, compatibility, decision):
    """Operator-only CAS at a quiescent boundary; supplied history never defaults to zero.

    Compatibility is the operator's receipt for all three writers, not provider
    attestation. The operational preflight owns verifying those runtime versions.
    """
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise PermissionError('workers cannot authorize enrollment')
    if not isinstance(decision, str) or not decision.strip() or len(decision) > 4096:
        raise ValueError('recorded operator decision required')
    for value, size in ((spec_digest, 64), (base_sha, 40), (target_sha, 40)):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}', value):
            raise ValueError('exact spec digest and base/target SHA required')
    if compatibility != {'cli': 1, 'gateway': 1, 'dashboard': 1}:
        raise ValueError('verified compatible CLI, gateway and dashboard receipts required')
    if implementer_maker not in {'openai', 'anthropic', 'google', 'nous', 'xai', 'deepseek'}:
        raise ValueError('known implementer maker required')
    if not isinstance(roster, list) or len(roster) != len(set(roster)) or not REQUIRED_LANES <= set(roster):
        raise ValueError('complete specialist, breaker and scope roster required')
    if set(roster) - REQUIRED_LANES - {'docs', 'system'}:
        raise ValueError('unknown review mandate')
    if not isinstance(consumed, dict) or set(consumed) != {'rounds', 'recovery', 'active_seconds'}:
        raise ValueError('known consumed budgets required')
    if any(type(consumed[k]) is not int or consumed[k] < 0 for k in ('rounds', 'recovery')):
        raise ValueError('invalid consumed counters')
    active = consumed['active_seconds']
    if type(active) not in (int, float) or not math.isfinite(active) or active < 0:
        raise ValueError('invalid consumed active time')
    policy = json.dumps(POLICY, sort_keys=True, separators=(',', ':'))
    with write_txn(conn):
        from hermes_cli.kanban_db import _append_event
        if get_attempt(conn, task_id):
            raise ValueError('task already enrolled; new task/policy/spec is not reset authority')
        from hermes_cli.kanban_postmortem import is_diagnostic
        if is_diagnostic(conn, task_id):
            raise ValueError('diagnostic tasks cannot enroll as implementations')
        board = conn.execute('SELECT board_id,schema_version FROM workflow_board WHERE singleton=1').fetchone()
        if tuple(board) != (board_id, 1):
            raise ValueError('board identity or schema mismatch')
        row = conn.execute('SELECT status,current_run_id,worker_pid FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row or tuple(row[:2]) != (expected_status, expected_run_id):
            raise ValueError('enrollment CAS lost')
        if expected_run_id is not None or row['worker_pid'] is not None or expected_status not in {'ready', 'review', 'blocked'}:
            raise ValueError('enrollment requires a quiescent safe boundary')
        exhausted = consumed['rounds'] >= POLICY['rounds'] or active >= POLICY['active_seconds']
        install_guard(conn)
        attempt_id = str(uuid.uuid4())
        conn.execute('''INSERT INTO review_attempts
            (id,task_id,board_id,spec_digest,policy_digest,policy,base_sha,target_sha,
             implementer_maker,roster,state,completed_rounds,recovery_used,active_seconds,
             decision,compatibility,hold_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (attempt_id, task_id, board_id, spec_digest, hashlib.sha256(policy.encode()).hexdigest(),
             policy, base_sha, target_sha, implementer_maker, json.dumps(roster),
             'held' if exhausted else 'preflight', consumed['rounds'], consumed['recovery'], active,
             decision, json.dumps(compatibility), 'migration_exhausted' if exhausted else None))
        _append_event(conn, task_id, 'review_enrolled', {'attempt_id': attempt_id})
    return get_attempt(conn, task_id)


def completion_allowed(conn, task_id, run_id=None, metadata=None):
    from hermes_cli.kanban_review_cohort import receive
    lane = receive(conn, task_id, run_id, metadata.get('bounded_review') if isinstance(metadata,dict) else None)
    if lane is not None:
        return lane
    attempt = get_attempt(conn, task_id)
    return attempt is None or attempt['state'] == 'approved'
