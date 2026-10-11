"""Explicit legacy-history decisions at the existing operator enrollment boundary."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid

from hermes_cli.kanban_db_connect import write_txn


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS review_legacy_adjudications (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, spec_digest TEXT NOT NULL,
        history_digest TEXT NOT NULL, disposition TEXT NOT NULL, receipt TEXT NOT NULL,
        history TEXT NOT NULL, UNIQUE(task_id,history_digest))''')


def inspect_history(conn, task_id):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError('legacy task not found')
    events = [dict(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id', (task_id,))]
    runs = [dict(row) for row in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (task_id,))]
    spec = hashlib.sha256(json.dumps({'title':task.title,'body':task.body},sort_keys=True).encode()).hexdigest()
    history = {'events':events, 'runs':runs, 'title':task.title, 'body':task.body,
               'status':task.status, 'current_run_id':task.current_run_id, 'worker_pid':task.worker_pid}
    descendants = conn.execute('''WITH RECURSIVE children(id) AS (
        SELECT child_id FROM task_links WHERE parent_id=? UNION
        SELECT l.child_id FROM task_links l JOIN children c ON l.parent_id=c.id)
        SELECT t.* FROM tasks t JOIN children c ON t.id=c.id ORDER BY t.id''', (task_id,)).fetchall()
    history['children'] = [{
        'task':dict(child),
        'events':[dict(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id',(child['id'],))],
        'runs':[dict(row) for row in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id',(child['id'],))],
    } for child in descendants]
    digest = hashlib.sha256(json.dumps(history,sort_keys=True).encode()).hexdigest()
    rounds = sum(event['kind'] == 'changes_requested' for event in events)
    review_runs = {event['run_id'] for event in events if event['kind'] == 'claimed'
                   and kb._json_dict(event['payload']).get('source_status') == 'review'}
    rounds += sum(run['outcome'] == 'completed' and (
        run['id'] in review_runs or kb._json_dict(run['metadata']).get('source_status') == 'review') for run in runs)
    recovery = sum(run['outcome'] in {'spawn_failed','crashed','timed_out'} for run in runs)
    lower = {'rounds':rounds,'recovery':recovery,'active_seconds':0}
    for row in conn.execute('SELECT receipt FROM review_legacy_adjudications WHERE task_id=?', (task_id,)):
        accepted = json.loads(row[0]).get('charged_consumed')
        if isinstance(accepted,dict):
            lower = {key:max(value,accepted[key]) for key,value in lower.items()}
    # These are proven lower bounds, not a reconstruction of undocumented old
    # cohort semantics. The operator must explicitly accept conservative counts.
    return {'task_id':task_id, 'board_id':conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
            'status':task.status, 'expected_run_id':task.current_run_id,
            'spec_digest':spec, 'history_digest':digest, 'history':kb.redact_review_value(history),
            'lower_bounds':lower}


def _quiescent(conn, task_id, observed):
    from hermes_cli.kanban_db import _host_prefix
    from hermes_cli.kanban_db_dispatch import _worker_alive
    runs = list(observed['history']['runs'])
    for child in observed['history']['children']:
        runs.extend(child['runs'])
    for run in runs:
        if run['worker_pid'] is not None and (
                not (run['claim_lock'] or '').startswith(_host_prefix())
                or _worker_alive(run['worker_pid'],run['worker_started_at'])):
            raise ValueError('legacy adjudication requires physical quiescence')
    if observed['expected_run_id'] is not None or observed['history']['worker_pid'] is not None:
        raise ValueError('legacy adjudication requires owner quiescence')
    if observed['status'] not in {'ready','blocked','done','scheduled'}:
        raise ValueError('ongoing frozen legacy review cannot be enrolled')
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL", (task_id,)).fetchone():
        raise ValueError('legacy adjudication requires settled prior runs')
    for child in observed['history']['children']:
        task = child['task']
        if (task['status'] in {'running','review'} or task['current_run_id'] is not None
                or task['worker_pid'] is not None or any(run['ended_at'] is None for run in child['runs'])):
            raise ValueError('ongoing frozen legacy child review cannot be enrolled')


def _counts(value, lower):
    if (not isinstance(value,dict) or set(value) != {'rounds','recovery','active_seconds'}
            or any(type(value[k]) is not int or value[k] < lower[k] for k in ('rounds','recovery'))
            or type(value['active_seconds']) not in (int,float)
            or not math.isfinite(value['active_seconds']) or value['active_seconds'] < lower['active_seconds']):
        raise ValueError('unknown history is not zero; conservative consumed counts required')


def adjudicate(conn, task_id, receipt):
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_review_readiness import _operator, verify
    from hermes_cli import kanban_db as kb
    _operator()
    if receipt == {'operation':'legacy-history','disposition':'inspect'}:
        return inspect_history(conn, task_id)
    fields = {'operation','disposition','history_digest','board_id','expected_status','expected_run_id',
              'spec_digest','base_sha','target_sha','approved_by','decision','consumed','compatibility',
              'implementer_maker','roster'}
    disposition = receipt.get('disposition')
    if disposition == 'successor':
        fields.add('successor')
    if set(receipt) != fields or disposition not in {'preserve_legacy','enroll','successor'}:
        raise ValueError('unsupported legacy-history decision schema')
    if (receipt['approved_by'] != 'Justin' or not isinstance(receipt['decision'],str)
            or not receipt['decision'].strip() or len(receipt['decision']) > 4096):
        raise ValueError('explicit scoped Justin decision required')
    if any(not isinstance(receipt[k],str) or not re.fullmatch('[0-9a-f]{40}',receipt[k]) for k in ('base_sha','target_sha')):
        raise ValueError('exact legacy base and target required')
    with write_txn(conn):
        from hermes_cli.kanban_postmortem import is_diagnostic
        if is_diagnostic(conn, task_id):
            raise ValueError('diagnostic tasks cannot be adjudicated as legacy implementation work')
        if state.get_attempt(conn, task_id):
            raise ValueError('managed lineage cannot be adjudicated as legacy')
        observed = inspect_history(conn, task_id)
        expected = {k:observed[k] for k in ('history_digest','board_id','spec_digest','expected_run_id')}
        expected['expected_status'] = observed['status']
        if any(receipt[k] != v for k,v in expected.items()):
            raise ValueError('legacy history CAS or approved ask mismatch')
        _quiescent(conn, task_id, observed)
        charged = receipt['consumed']
        if disposition == 'preserve_legacy' and charged is not None:
            _counts(charged, observed['lower_bounds'])
        if disposition == 'successor' and charged is None:
            from hermes_cli.config import load_config
            policy = load_config()['kanban']['review_feedback']
            charged = {k:policy[k] for k in ('rounds','recovery','active_seconds')}
            charged = {k:max(v,observed['lower_bounds'][k]) for k,v in charged.items()}
        if disposition != 'preserve_legacy':
            verify(conn, receipt['compatibility'])
            _counts(charged, observed['lower_bounds'])
            if observed['history']['runs'] and not any(charged.values()):
                raise ValueError('unknown historical execution cannot silently become zero')
        safe = kb.redact_review_value(receipt)
        safe['charged_consumed'] = charged
        conn.execute('INSERT INTO review_legacy_adjudications VALUES(?,?,?,?,?,?,?)',
                     (str(uuid.uuid4()),task_id,receipt['spec_digest'],receipt['history_digest'],disposition,
                      json.dumps(safe,sort_keys=True),json.dumps(observed['history'],sort_keys=True)))
        result = {'disposition':disposition,'task_id':task_id,'history_digest':receipt['history_digest']}
        if disposition != 'preserve_legacy':
            # A clean old verdict does not approve the new roster. Even adoption
            # of a completed card begins preflight (or an exhausted hold).
            args = {k:receipt[k] for k in ('expected_run_id','board_id','spec_digest','base_sha','target_sha',
                    'implementer_maker','roster','consumed','compatibility','decision')}
            args['expected_status'] = kb.get_task(conn,task_id).status
            args['expected_assignee'] = kb.get_task(conn,task_id).assignee
            args['consumed'] = charged
            result['attempt'] = state.enroll_review(conn, task_id, **args)
            if disposition == 'successor':
                result['successor'] = _legacy_successor(conn, task_id, result['attempt'], receipt, observed)
                result['attempt'] = state.get_attempt(conn,task_id)
        kb._append_event(conn, task_id, 'legacy_history_adjudicated',
                         {'disposition':disposition,'history_digest':receipt['history_digest']})
    return result


def _legacy_successor(conn, task_id, attempt, receipt, observed):
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_review_operator import decide
    new = receipt['successor']
    fields = {'task_id','base_sha','target_sha','allowance','remaining_finding_events'}
    findings = [event['id'] for event in observed['history']['events'] if event['kind'] == 'changes_requested']
    if not isinstance(new,dict) or set(new) != fields or new['remaining_finding_events'] != findings:
        raise ValueError('successor must retain every legacy finding event and uncertainty')
    if attempt['state'] != 'held':
        state.hold(conn, attempt, 'legacy_successor_authorized')
    current = state.get_attempt(conn,task_id)
    decision = {k:current[k] for k in ('board_id','spec_digest','base_sha','target_sha')}
    decision.update(operation='successor',attempt_id=current['id'],expected_version=current['version'],
                    approved_by=receipt['approved_by'],decision=receipt['decision'],findings=[],
                    successor={k:new[k] for k in ('task_id','base_sha','target_sha','allowance')})
    decision['successor']['compatibility'] = receipt['compatibility']
    decide(conn,task_id,decision)
    return state.get_attempt(conn,new['task_id'])


def require_adjudication(conn, task_id, spec_digest, consumed):
    """Fresh intake stays simple; existing execution needs a pinned decision."""
    prior = conn.execute('SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1', (task_id,)).fetchone()
    observed = inspect_history(conn,task_id)
    if conn.execute('SELECT 1 FROM review_legacy_adjudications WHERE spec_digest=? AND task_id!=?',
                    (observed['spec_digest'],task_id)).fetchone():
        raise ValueError('legacy ask already has history; recreation is not successor authority')
    row = conn.execute('SELECT receipt,disposition FROM review_legacy_adjudications WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
    if not prior and not row:
        return
    if not row or row['disposition'] not in {'enroll','successor'}:
        raise ValueError('legacy execution requires explicit history adjudication')
    accepted = json.loads(row['receipt'])
    if accepted['spec_digest'] != spec_digest or accepted['charged_consumed'] != consumed:
        raise ValueError('legacy adjudication does not authorize these counts or ask')
    # Outside the original transaction, the journal event changes the digest.
    if inspect_history(conn,task_id)['history_digest'] != accepted['history_digest']:
        raise ValueError('legacy history changed after adjudication')
