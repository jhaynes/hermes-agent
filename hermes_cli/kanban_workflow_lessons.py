"""Typed procedural-evidence proposals and independent deterministic validation."""
from __future__ import annotations

import json
import math
import uuid


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_lessons (
        id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, source_event INTEGER NOT NULL,
        author_task TEXT NOT NULL, validator_task TEXT UNIQUE, validator_run INTEGER,
        status TEXT NOT NULL, record TEXT NOT NULL, before_hash TEXT, after_hash TEXT,
        UNIQUE(incident_id,source_event,record))''')
    from hermes_cli.kanban_lesson_apply import initialize as initialize_updates
    initialize_updates(conn)


def admissible(proposal):
    if not isinstance(proposal,dict) or set(proposal)!={'kind','record','approval_required'}:
        return False
    record=proposal['record']
    return (proposal['kind']=='procedural_evidence' and proposal['approval_required'] is False
            and isinstance(record,dict)
            and set(record)=={'procedure_id','failure_shape','source_event','required_evidence','invocation'}
            and record['procedure_id']=='record-worker-deadline'
            and record['failure_shape']=='worker-timeout'
            and type(record['source_event']) is int and record['source_event']>0
            and record['required_evidence']==['elapsed_seconds','limit_seconds']
            and record['invocation']=='hermes kanban runs <task-id> --json')


def propose(conn, incident, author_task, proposal):
    from hermes_cli import kanban_db as kb
    # The reporter runs in its own home, not the dispatcher policy's home.
    # Routing is reserved at queue time, never inherited from author prose.
    job = conn.execute('SELECT validator_profile FROM workflow_postmortems WHERE task_id=?', (author_task,)).fetchone()
    profile = job['validator_profile'] if job else None
    if not admissible(proposal) or not profile:
        conn.execute("UPDATE workflow_incidents SET lesson_status='pending_approval' WHERE id=?",(incident['id'],))
        return
    record=proposal['record']
    if record['source_event'] not in json.loads(incident['source_events']):
        conn.execute("UPDATE workflow_incidents SET lesson_status='pending_approval' WHERE id=?",(incident['id'],))
        return
    encoded=json.dumps(record,sort_keys=True)
    existing=conn.execute('SELECT 1 FROM workflow_lessons WHERE incident_id=? AND source_event=? AND record=?',
                          (incident['id'],record['source_event'],encoded)).fetchone()
    if existing:
        return
    conn.execute('INSERT INTO workflow_lessons(id,incident_id,source_event,author_task,validator_task,status,record) VALUES(?,?,?,?,?,?,?)',
                 (str(uuid.uuid4()),incident['id'],record['source_event'],author_task,None,'proposed',encoded))


def queue_validators(conn):
    """Only the dispatcher creates validators; a reporter merely reserves data."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        rows = conn.execute('''SELECT l.*, p.validator_profile FROM workflow_lessons l
            JOIN workflow_postmortems p ON p.task_id=l.author_task
            WHERE l.status='proposed' AND l.validator_task IS NULL LIMIT 16''').fetchall()
        for row in rows:
            task = kb.create_task(conn, title='Validate procedural evidence', body=row['record'],
                                  assignee=row['validator_profile'], max_runtime_seconds=600,
                                  max_retries=1, goal_mode=False)
            conn.execute('UPDATE workflow_lessons SET validator_task=? WHERE id=?', (task, row['id']))


def is_validator(conn, task_id):
    return conn.execute('SELECT 1 FROM workflow_lessons WHERE validator_task=?',(task_id,)).fetchone() is not None


def receive(conn, task_id, run_id, receipt):
    lesson=conn.execute('SELECT * FROM workflow_lessons WHERE validator_task=?',(task_id,)).fetchone()
    if not lesson:
        return None
    from hermes_cli.kanban_postmortem import supported
    if not supported(conn, task_id=task_id):
        return False
    if lesson['status']!='proposed' or receipt!={'source_event':lesson['source_event'],'result':'reproduced'}:
        return False
    validator=conn.execute('SELECT profile FROM task_runs WHERE id=? AND task_id=? AND ended_at IS NULL',(run_id,task_id)).fetchone()
    author=conn.execute('SELECT profile FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',(lesson['author_task'],)).fetchone()
    if not validator or not author or not validator[0] or validator[0]==author[0]:
        return False
    source=conn.execute('SELECT kind,payload FROM task_events WHERE id=?',(lesson['source_event'],)).fetchone()
    payload=json.loads(source['payload'] or '{}') if source else {}
    if not source or source['kind']!='timed_out' or not isinstance(payload,dict):
        return False
    elapsed,limit=payload.get('elapsed_seconds'),payload.get('limit_seconds')
    # Replay only the recorded numeric comparison, never a command from a log.
    if not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in (elapsed,limit)) or elapsed<limit:
        return False
    conn.execute("UPDATE workflow_lessons SET status='validated',validator_run=? WHERE id=?",(run_id,lesson['id']))
    conn.execute("UPDATE workflow_incidents SET lesson_status='validated' WHERE id=?",(lesson['incident_id'],))
    return True


def failed(conn, task_id, outcome):
    if outcome=='completed':
        return
    conn.execute("UPDATE workflow_lessons SET status='pending_approval' WHERE validator_task=? AND status='proposed'",(task_id,))
    conn.execute("UPDATE workflow_incidents SET lesson_status='pending_approval' WHERE id IN (SELECT incident_id FROM workflow_lessons WHERE validator_task=?)",(task_id,))


def reevaluate_recurrence(conn, incident_id, fingerprint):
    # An applied tip is not evidence of prevention. Keep its append-only audit
    # but stop recommending it automatically after the same failure recurs.
    changed = conn.execute("""UPDATE workflow_lessons SET status='pending_approval'
        WHERE status='applied' AND incident_id IN
            (SELECT id FROM workflow_incidents WHERE fingerprint=?)""", (fingerprint,)).rowcount
    if changed:
        conn.execute("""UPDATE workflow_incidents SET lesson_status='pending_approval'
            WHERE id=? OR (fingerprint=? AND lesson_status='applied')""", (incident_id, fingerprint))
