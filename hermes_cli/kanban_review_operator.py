"""Recorded operator decisions at quiescent managed phase boundaries.

This is the trusted operator CLI boundary, not authentication of arbitrary Python
running as the account owner. Worker contexts cannot exercise it.
"""
import hashlib
import json
import os
import re
import uuid

from hermes_cli.kanban_db_connect import write_txn


def initialize(conn):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(review_attempts)')}
    if 'resume_state' not in columns:
        conn.execute('ALTER TABLE review_attempts ADD COLUMN resume_state TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS review_decisions (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, version INTEGER NOT NULL,
        operation TEXT NOT NULL, receipt TEXT NOT NULL,
        UNIQUE(attempt_id,version))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS review_successors (
        predecessor_id TEXT PRIMARY KEY, successor_id TEXT NOT NULL UNIQUE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS review_dispositions (
        finding_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, receipt TEXT NOT NULL)''')


def decide(conn, task_id, receipt):
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_review_guards import has_live_worker, identity_valid
    from hermes_cli import kanban_db as kb
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise ValueError('workers cannot authorize managed review decisions')
    fields = {'operation','attempt_id','expected_version','board_id','spec_digest',
              'base_sha','target_sha','approved_by','decision','findings'}
    operation = receipt.get('operation')
    extra = {'amend': 'amendment', 'successor': 'successor', 'reject_finding': 'disposition'}.get(operation)
    if extra:
        fields.add(extra)
    if set(receipt) != fields or operation not in _OPERATIONS:
        raise ValueError('unsupported operator decision schema')
    if (receipt['approved_by'] != 'Justin' or not isinstance(receipt['decision'], str)
            or not receipt['decision'].strip() or len(receipt['decision']) > 4096):
        raise ValueError('explicit scoped Justin decision required')
    with write_txn(conn):
        attempt = state.get_attempt(conn, task_id)
        if not attempt or attempt['task_id'] != task_id or not identity_valid(conn, attempt):
            raise ValueError('current managed owner identity required')
        expected = {k:attempt[k] for k in ('board_id','spec_digest','base_sha','target_sha')}
        expected.update(attempt_id=attempt['id'], expected_version=attempt['version'])
        if any(receipt[k] != value for k,value in expected.items()):
            raise ValueError('operator decision CAS or pinned snapshot mismatch')
        if has_live_worker(conn, attempt['id']) or conn.execute(
                "SELECT 1 FROM review_actions WHERE attempt_id=? AND state='running'", (attempt['id'],)).fetchone():
            raise ValueError('operator decision requires physical quiescence')
        from hermes_cli.kanban_review_cohort import prior_findings
        findings = {f['finding_id'] for mandate in attempt['roster']
                    for f in prior_findings(conn, attempt['id'], mandate)}
        if (not isinstance(receipt['findings'], list) or any(not isinstance(f,str) for f in receipt['findings'])
                or len(receipt['findings']) != len(set(receipt['findings']))
                or set(receipt['findings']) != findings):
            raise ValueError('decision must retain every recorded finding')
        if attempt['state'] in {'cancelled','approved'} and operation != 'successor':
            raise ValueError('terminal attempt requires explicit successor, not resumption')
        _OPERATIONS[operation](conn, task_id, attempt, receipt)
        safe = kb.redact_review_value(receipt)
        conn.execute('INSERT INTO review_decisions VALUES(?,?,?,?,?)',
                     (str(uuid.uuid4()), attempt['id'], attempt['version'], operation, json.dumps(safe, sort_keys=True)))
        kb._append_event(conn, task_id, 'review_operator_decision', {'attempt_id':attempt['id'], 'operation':operation, 'version':attempt['version']})
    return state.get_attempt(conn, task_id)


def _cancel(conn, task_id, attempt, receipt):
    conn.execute("UPDATE review_attempts SET state='cancelled',version=version+1,hold_reason='operator_cancelled' WHERE id=?", (attempt['id'],))
    conn.execute("UPDATE review_actions SET state='cancelled' WHERE attempt_id=? AND state='reserved'", (attempt['id'],))
    conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (task_id,))


def _resume(conn, task_id, attempt, receipt):
    from hermes_cli import kanban_db as kb
    if attempt['state'] != 'held' or attempt['resume_state'] not in {'preflight','reviewing','repair'}:
        raise ValueError('known held phase required; uncertain history needs adjudication')
    if (attempt['completed_rounds'] >= attempt['policy']['rounds']
            or attempt['active_seconds'] >= attempt['policy']['active_seconds']
            or attempt['hold_reason'] in {'recovery_exhausted','clock_reconciliation_required','workflow_identity_mismatch'}
            or conn.execute('SELECT 1 FROM review_successors WHERE predecessor_id=?', (attempt['id'],)).fetchone()):
        raise ValueError('exhausted or uncertain attempt requires a scoped successor decision')
    conn.execute('UPDATE review_attempts SET state=resume_state,hold_reason=NULL,version=version+1 WHERE id=?', (attempt['id'],))
    conn.execute("UPDATE tasks SET status=?,block_kind=NULL WHERE id=?",
                 ('review' if attempt['resume_state']=='reviewing' else kb._landing_status_after_parents(conn, task_id), task_id))


def _amend(conn, task_id, attempt, receipt):
    amendment = receipt['amendment']
    if (not isinstance(amendment,dict) or set(amendment) != {'ask','spec_digest'}
            or not isinstance(amendment['ask'],str) or not amendment['ask'].strip()
            or len(amendment['ask']) > 16000
            or hashlib.sha256(amendment['ask'].encode()).hexdigest() != amendment['spec_digest']):
        raise ValueError('amendment requires exact approved ask and matching digest')
    if (attempt['state'] != 'held' or attempt['resume_state'] not in {'preflight','repair'}
            or conn.execute("SELECT 1 FROM review_actions WHERE attempt_id=? AND state='reserved'", (attempt['id'],)).fetchone()
            or conn.execute("SELECT 1 FROM review_rounds WHERE attempt_id=? AND state='running'", (attempt['id'],)).fetchone()):
        raise ValueError('amendment cannot alter a frozen or reserved round')
    conn.execute('UPDATE review_attempts SET spec_digest=?,version=version+1 WHERE id=?', (amendment['spec_digest'], attempt['id']))


def _successor(conn, task_id, attempt, receipt):
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_postmortem import is_diagnostic
    new = receipt['successor']
    if (attempt['state'] not in {'held','cancelled'} or not isinstance(new,dict)
            or set(new) != {'task_id','base_sha','target_sha','allowance'}):
        raise ValueError('successor requires a held predecessor and exact finite allowance')
    allowance = new['allowance']
    if (not isinstance(allowance,dict) or set(allowance) != {'rounds','recovery','active_seconds'}
            or any(type(allowance[k]) is not int or allowance[k] < (0 if k=='recovery' else 1) for k in allowance)
            or any(not isinstance(new[k],str) or not re.fullmatch('[0-9a-f]{40}',new[k]) for k in ('base_sha','target_sha'))):
        raise ValueError('successor requires valid SHA pair and finite allowance')
    target = conn.execute('SELECT status,current_run_id,worker_pid FROM tasks WHERE id=?', (new['task_id'],)).fetchone()
    if (not target or target['status'] not in {'ready','blocked','review'} or target['current_run_id'] is not None
            or target['worker_pid'] is not None or state.get_attempt(conn,new['task_id'])
            or is_diagnostic(conn, new['task_id'])
            or conn.execute('SELECT 1 FROM review_successors WHERE predecessor_id=?',(attempt['id'],)).fetchone()):
        raise ValueError('successor target must be a distinct quiescent unenrolled task')
    policy = json.dumps({'version':1, **allowance},sort_keys=True,separators=(',',':'))
    new_id = str(uuid.uuid4())
    conn.execute('''INSERT INTO review_attempts
        (id,task_id,board_id,spec_digest,policy_digest,policy,base_sha,target_sha,implementer_maker,
         roster,state,completed_rounds,recovery_used,active_seconds,decision,compatibility)
        VALUES(?,?,?,?,?,?,?,?,?,?,'preflight',0,0,0,?,?)''',
        (new_id,new['task_id'],attempt['board_id'],attempt['spec_digest'],hashlib.sha256(policy.encode()).hexdigest(),policy,
         new['base_sha'],new['target_sha'],attempt['implementer_maker'],json.dumps(attempt['roster']),
         receipt['decision'],json.dumps(attempt['compatibility'])))
    conn.execute('INSERT INTO review_successors VALUES(?,?)',(attempt['id'],new_id))
    conn.execute("UPDATE review_attempts SET version=version+1 WHERE id=?",(attempt['id'],))


def _reject_finding(conn, task_id, attempt, receipt):
    from hermes_cli.kanban_review_evidence import evidence_valid
    from hermes_cli import kanban_db as kb
    disposition = receipt['disposition']
    if (attempt['state'] not in {'held','repair'} or not isinstance(disposition,dict)
            or set(disposition) != {'finding_id','evidence'}
            or disposition['finding_id'] not in receipt['findings']
            or not evidence_valid(disposition['evidence'])):
        raise ValueError('finding rejection requires parent evidence at a settled round')
    safe = kb.redact_review_value({'status':'rejected', **disposition})
    if conn.execute('SELECT 1 FROM review_dispositions WHERE finding_id=?',(disposition['finding_id'],)).fetchone():
        raise ValueError('finding disposition already recorded')
    conn.execute('INSERT INTO review_dispositions VALUES(?,?,?)',
                 (disposition['finding_id'],attempt['id'],json.dumps(safe,sort_keys=True)))
    conn.execute('UPDATE review_attempts SET version=version+1 WHERE id=?',(attempt['id'],))


_OPERATIONS = {'cancel':_cancel, 'resume':_resume, 'amend':_amend, 'successor':_successor,
               'reject_finding':_reject_finding}


def approved_ask(conn, attempt):
    lineage = '''WITH RECURSIVE lineage(id) AS (SELECT ? UNION
        SELECT s.predecessor_id FROM review_successors s JOIN lineage l ON s.successor_id=l.id) '''
    root = conn.execute(lineage + '''SELECT t.title,t.body FROM review_attempts a
        JOIN tasks t ON t.id=a.task_id WHERE a.id IN (SELECT id FROM lineage)
        AND NOT EXISTS(SELECT 1 FROM review_successors s WHERE s.successor_id=a.id)''', (attempt['id'],)).fetchone()
    amendment = conn.execute(lineage + '''SELECT receipt FROM review_decisions
        WHERE attempt_id IN (SELECT id FROM lineage) AND operation='amend'
        AND json_extract(receipt,'$.amendment.spec_digest')=? ORDER BY rowid DESC LIMIT 1''',
        (attempt['id'],attempt['spec_digest'])).fetchone()
    return {'title':root['title'], 'body':root['body'],
            'amendment':json.loads(amendment[0])['amendment'] if amendment else None}
