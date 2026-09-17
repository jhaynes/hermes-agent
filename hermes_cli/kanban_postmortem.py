"""Finite diagnostic jobs on the existing Kanban queue; never implementation owners."""
from __future__ import annotations

import json
import math
import time

from hermes_cli.kanban_db_connect import write_txn


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_postmortems (
        incident_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
        runs_started INTEGER NOT NULL DEFAULT 0, active_seconds REAL NOT NULL DEFAULT 0,
        started_monotonic REAL, boot_id TEXT, run_id INTEGER)''')


def queue_reports(conn):
    from hermes_cli.config import load_config
    from hermes_cli import kanban_db as kb
    config = load_config().get('kanban', {}).get('review_feedback', {})
    profile = config.get('postmortem_profile')
    if not profile:
        return
    with write_txn(conn):
        rows = conn.execute('''SELECT i.* FROM workflow_incidents i
            WHERE report_status='queued' AND NOT EXISTS(
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
            conn.execute('INSERT INTO workflow_postmortems(incident_id,task_id) VALUES(?,?)', (incident['id'],task_id))
            kb._insert_comment(conn, incident['task_id'], 'workflow',
                               f"Incident {incident['id']}: diagnostic {task_id}; implementation remains held.", int(time.time()))


def claim_allowed(conn, task_id):
    row = conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return True
    return row['runs_started'] < 2 and row['active_seconds'] < 600


def claimed(conn, task_id, run_id):
    import psutil
    row = conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return
    cap = max(1, math.floor(600-row['active_seconds']))
    conn.execute('''UPDATE workflow_postmortems SET runs_started=runs_started+1,
        started_monotonic=?,boot_id=?,run_id=? WHERE task_id=?''',
        (time.monotonic(),str(psutil.boot_time()),run_id,task_id))
    conn.execute("UPDATE workflow_incidents SET report_status='running' WHERE id=?", (row['incident_id'],))
    conn.execute('UPDATE tasks SET max_runtime_seconds=? WHERE id=?', (cap,task_id))
    conn.execute('UPDATE task_runs SET max_runtime_seconds=? WHERE id=?', (cap,run_id))


def released(conn, task_id, run_id, outcome):
    import psutil
    row = conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=? AND run_id=?', (task_id,run_id)).fetchone()
    if not row or row['started_monotonic'] is None:
        return
    elapsed = time.monotonic()-row['started_monotonic']
    if row['boot_id'] != str(psutil.boot_time()) or elapsed < 0:
        elapsed = 600
    total = row['active_seconds'] + elapsed
    conn.execute('UPDATE workflow_postmortems SET active_seconds=?,started_monotonic=NULL WHERE task_id=?', (total,task_id))
    if outcome != 'completed':
        failed = row['runs_started'] >= 2 or total >= 600
        conn.execute('UPDATE workflow_incidents SET report_status=? WHERE id=?', ('synthesis_failed' if failed else 'queued',row['incident_id']))
        if failed:
            conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (task_id,))


def is_diagnostic(conn, task_id):
    return conn.execute('SELECT 1 FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone() is not None
