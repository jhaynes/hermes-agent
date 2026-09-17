"""Finite diagnostic jobs on the existing Kanban queue; never implementation owners."""
from __future__ import annotations

import json
import hashlib
import math
import time

from hermes_cli.kanban_db_connect import write_txn


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_postmortems (
        incident_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
        runs_started INTEGER NOT NULL DEFAULT 0, active_seconds REAL NOT NULL DEFAULT 0,
        started_monotonic REAL, boot_id TEXT, run_id INTEGER)''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(workflow_postmortems)')}
    if 'validator_profile' not in columns:
        conn.execute('ALTER TABLE workflow_postmortems ADD COLUMN validator_profile TEXT')
    if 'deadline' not in columns:
        conn.execute('ALTER TABLE workflow_postmortems ADD COLUMN deadline REAL')
    if 'failure_kind' not in columns:
        conn.execute('ALTER TABLE workflow_postmortems ADD COLUMN failure_kind TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_report_artifacts (
        incident_id TEXT PRIMARY KEY, attachment_id INTEGER NOT NULL, digest TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_report_publication_failures (
        incident_id TEXT PRIMARY KEY, error_kind TEXT NOT NULL, attempts INTEGER NOT NULL)''')


def queue_reports(conn):
    if not supported(conn):
        return
    from hermes_cli.config import load_config
    from hermes_cli import kanban_db as kb
    try:
        publish_reports(conn)
    except (OSError, ValueError) as exc:
        # Outbox I/O cannot prevent supervising running workers. Keep a typed
        # publication failure, never an exception string containing a path/secret.
        with write_txn(conn):
            conn.execute('''INSERT INTO workflow_report_publication_failures
                SELECT i.id, ?, 1 FROM workflow_incidents i WHERE i.report_status='complete'
                AND i.classification!='expected_wait' AND NOT EXISTS (
                    SELECT 1 FROM workflow_report_artifacts a WHERE a.incident_id=i.id)
                ON CONFLICT(incident_id) DO UPDATE SET attempts=attempts+1,error_kind=excluded.error_kind''',
                ('io_failure' if isinstance(exc, OSError) else 'artifact_mismatch',))
    from hermes_cli.kanban_workflow_lessons import queue_validators
    queue_validators(conn)
    from hermes_cli.kanban_lesson_apply import apply_next
    try:
        apply_next(conn)
    except (OSError,ValueError):
        # A stale/reference I/O failure is a learning hold, not permission to
        # stop supervising implementation deadlines or replay unsafe content.
        with write_txn(conn):
            conn.execute("UPDATE workflow_lessons SET status='pending_approval' WHERE status='validated'")
            conn.execute("UPDATE workflow_lesson_updates SET status='pending_approval' WHERE status='applying'")
            conn.execute("UPDATE workflow_incidents SET lesson_status='pending_approval' WHERE id IN (SELECT incident_id FROM workflow_lessons WHERE status='pending_approval')")
    config = load_config().get('kanban', {}).get('review_feedback', {})
    profile = config.get('postmortem_profile')
    if not profile:
        return
    with write_txn(conn):
        rows = conn.execute('''SELECT i.* FROM workflow_incidents i
            WHERE report_status='queued' AND i.board_id=(SELECT board_id FROM workflow_board)
            AND NOT EXISTS(
              SELECT 1 FROM workflow_postmortems p WHERE p.incident_id=i.id)
            ORDER BY i.rowid LIMIT 16''').fetchall()
        for incident in rows:
            # Safe identifier-only evidence is deliberately weaker than a raw log.
            body = json.dumps({'schema':1,'incident_id':incident['id'],
                'source_events':json.loads(incident['source_events']),
                'classification':incident['classification'],
                'mandate':'Read-only synthesis: cite receipts; separate facts and hypotheses; do not execute recovery or change policy.'})
            task_id = kb.create_task(conn, title='Bounded workflow postmortem', body=body,
                                     assignee=profile, max_runtime_seconds=600,
                                     max_retries=2, goal_mode=False)
            conn.execute('INSERT INTO workflow_postmortems(incident_id,task_id,validator_profile) VALUES(?,?,?)',
                         (incident['id'], task_id, config.get('validator_profile')))
            kb._insert_comment(conn, incident['task_id'], 'workflow',
                               f"Incident {incident['id']}: diagnostic {task_id}; owner state is unchanged.", int(time.time()))


def claim_allowed(conn, task_id):
    if is_diagnostic(conn, task_id) and not supported(conn, task_id=task_id):
        return False
    from hermes_cli.kanban_diagnostic_clock import settle
    row = settle(conn, task_id)
    if not row:
        lesson = conn.execute('SELECT status FROM workflow_lessons WHERE validator_task=?', (task_id,)).fetchone()
        if lesson:
            return lesson['status'] == 'proposed' and not conn.execute(
                'SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1', (task_id,)).fetchone()
        return True
    status = conn.execute('SELECT report_status FROM workflow_incidents WHERE id=?', (row['incident_id'],)).fetchone()[0]
    return (row['runs_started'] < 2 and row['active_seconds'] < 600
            and (row['runs_started'] == 0 or row['failure_kind'] == 'infrastructure')
            and row['started_monotonic'] is None and status == 'queued')


def claimed(conn, task_id, run_id):
    import psutil
    row = conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return
    cap = max(1, math.floor(600-row['active_seconds']))
    conn.execute('''UPDATE workflow_postmortems SET runs_started=runs_started+1,
        started_monotonic=?,boot_id=?,run_id=?,deadline=? WHERE task_id=?''',
        (time.monotonic(),str(psutil.boot_time()),run_id,time.time()+cap,task_id))
    conn.execute("UPDATE workflow_incidents SET report_status='running' WHERE id=?", (row['incident_id'],))
    conn.execute('UPDATE tasks SET max_runtime_seconds=? WHERE id=?', (cap,task_id))
    conn.execute('UPDATE task_runs SET max_runtime_seconds=? WHERE id=?', (cap,run_id))


def released(conn, task_id, run_id, outcome, metadata=None):
    from hermes_cli.kanban_diagnostic_clock import settle
    row = conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=? AND run_id=?', (task_id,run_id)).fetchone()
    if not row or row['started_monotonic'] is None:
        return
    row = settle(conn, task_id)
    total = row['active_seconds']
    if outcome != 'completed':
        metadata = metadata if isinstance(metadata, dict) else {}
        elapsed, limit = metadata.get('elapsed_seconds'), metadata.get('limit_seconds')
        supervisor_timeout = (outcome == 'timed_out'
            and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (elapsed, limit))
            and elapsed >= limit)
        infrastructure = outcome in {'spawn_failed', 'rate_limited'} or supervisor_timeout or (
            outcome == 'crashed' and metadata.get('exit_kind') == 'signaled'
            and type(metadata.get('exit_code')) is int)
        kind = ('content_rejected' if row['failure_kind'] == 'content_rejected' else
                'infrastructure' if infrastructure else 'unverified_failure')
        conn.execute('UPDATE workflow_postmortems SET failure_kind=? WHERE task_id=?', (kind, task_id))
        failed = kind != 'infrastructure' or row['runs_started'] >= 2 or total >= 600
        conn.execute('UPDATE workflow_incidents SET report_status=? WHERE id=?', ('synthesis_failed' if failed else 'queued',row['incident_id']))
        if failed:
            conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (task_id,))


def is_diagnostic(conn, task_id):
    from hermes_cli.kanban_workflow_lessons import is_validator
    return (conn.execute('SELECT 1 FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone() is not None
            or is_validator(conn,task_id))


def supported(conn, *, task_id=None):
    board = conn.execute('SELECT board_id,schema_version FROM workflow_board WHERE singleton=1').fetchone()
    if not board or board['schema_version'] != 1:
        return False
    if task_id is None:
        return True
    incident = conn.execute('''SELECT i.board_id FROM workflow_incidents i WHERE i.id IN (
        SELECT incident_id FROM workflow_postmortems WHERE task_id=? UNION
        SELECT incident_id FROM workflow_lessons WHERE validator_task=?)''', (task_id, task_id)).fetchone()
    return incident is not None and incident['board_id'] == board['board_id']


def receive(conn, task_id, run_id, report):
    job=conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=?',(task_id,)).fetchone()
    if not job:
        return None
    if not supported(conn, task_id=task_id):
        return False
    from hermes_cli.kanban_diagnostic_clock import settle
    job = settle(conn, task_id)
    if job['active_seconds'] >= 600 or job['deadline'] is None or time.time() >= job['deadline']:
        return False
    if run_id is None or job['run_id']!=run_id or not isinstance(report,dict):
        return False
    current = conn.execute('''SELECT 1 FROM tasks t JOIN task_runs r ON r.id=t.current_run_id
        WHERE t.id=? AND t.status='running' AND r.id=? AND r.ended_at IS NULL''', (task_id, run_id)).fetchone()
    if not current:
        return False
    incident=conn.execute('SELECT * FROM workflow_incidents WHERE id=?',(job['incident_id'],)).fetchone()
    if incident['report_status'] != 'running':
        return False
    from hermes_cli.kanban_diagnostic_report import valid
    if not valid(conn, incident, report):
        conn.execute("UPDATE workflow_postmortems SET failure_kind='content_rejected' WHERE task_id=?", (task_id,))
        return False
    change=report['proposed_change']
    from hermes_cli.kanban_workflow_lessons import propose
    encoded=json.dumps(report,sort_keys=True)
    if len(encoded.encode())>32768:
        return False
    conn.execute("UPDATE workflow_incidents SET report=?,report_status='complete' WHERE id=?", (encoded,incident['id']))
    if change is not None and report['facts']:
        propose(conn,incident,task_id,change)
    elif change is not None:
        conn.execute("UPDATE workflow_incidents SET lesson_status='pending_approval' WHERE id=?", (incident['id'],))
    return True


def publish_reports(conn):
    """Transactional outbox; a restart recovers a stored blob before making another."""
    from hermes_cli import kanban_db as kb
    if not supported(conn):
        return
    with write_txn(conn):
        rows=conn.execute('''SELECT * FROM workflow_incidents i WHERE report_status='complete'
            AND i.board_id=(SELECT board_id FROM workflow_board)
            AND classification IN ('failure','stalled_queue') AND NOT EXISTS(
            SELECT 1 FROM workflow_report_artifacts a WHERE a.incident_id=i.id) LIMIT 16''').fetchall()
        for incident in rows:
            from hermes_cli.kanban_diagnostic_report import valid
            if (not isinstance(incident['report'], str) or len(incident['report'].encode()) > 32768
                    or not valid(conn, incident, json.loads(incident['report']))):
                raise ValueError('unvalidated report cannot be published')
            data=incident['report'].encode()
            digest=hashlib.sha256(data).hexdigest()
            filename=f'postmortem-{digest}.json'
            existing=conn.execute('SELECT id FROM task_attachments WHERE task_id=? AND filename=? AND uploaded_by=?',
                                  (incident['task_id'],filename,'workflow')).fetchone()
            path = kb.task_attachments_dir(incident['task_id']) / filename
            if existing:
                attachment = existing[0]
            elif path.exists() or path.is_symlink():
                # A crash can commit the blob but roll back its metadata row.
                # Adopt only the exact generated name and verified bytes.
                if path.is_symlink() or path.stat().st_size != len(data) or path.read_bytes() != data:
                    raise ValueError('postmortem artifact mismatch; operator inspection required')
                attachment = kb.add_attachment(conn, incident['task_id'], filename=filename,
                    stored_path=str(path.resolve()), content_type='application/json', size=len(data), uploaded_by='workflow')
            else:
                attachment = kb.store_attachment_bytes(conn,incident['task_id'],filename,data,content_type='application/json',uploaded_by='workflow')
            stored = kb.get_attachment(conn, attachment)
            from pathlib import Path
            blob = Path(stored.stored_path)
            if blob.is_symlink() or blob.stat().st_size != len(data) or blob.read_bytes() != data:
                raise ValueError('postmortem artifact readback mismatch')
            conn.execute('INSERT INTO workflow_report_artifacts VALUES(?,?,?)',(incident['id'],attachment,digest))
            conn.execute('DELETE FROM workflow_report_publication_failures WHERE incident_id=?', (incident['id'],))
            kb._insert_comment(conn,incident['task_id'],'workflow',
                               f"Postmortem {incident['id']} attached ({digest}); incident remains {incident['state']}.",int(time.time()))
            if incident['state'] != 'acknowledged':
                kb._append_event(conn, incident['task_id'], 'postmortem_report',
                                 {'incident_id': incident['id'], 'attachment_id': attachment})
