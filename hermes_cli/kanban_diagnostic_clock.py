"""Diagnostic clock settlement including closed runs with surviving processes."""
from __future__ import annotations

import time
import psutil


def settle(conn, task_id):
    row = conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        return None
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (row['run_id'],)).fetchone()
    from hermes_cli.kanban_db_dispatch import _worker_alive
    from hermes_cli.kanban_db import _host_prefix
    live = bool(run and run['worker_pid'] and (
        not str(run['claim_lock'] or '').startswith(_host_prefix())
        or _worker_alive(run['worker_pid'], run['worker_started_at'])))
    mono, wall = time.monotonic(), time.time()
    total = row['active_seconds']
    uncertain = False
    if row['started_monotonic'] is not None:
        elapsed = mono - row['started_monotonic']
        uncertain = row['boot_id'] != str(psutil.boot_time()) or elapsed < 0
        total += 600 if uncertain else elapsed
    running = live or bool(run and run['ended_at'] is None)
    deadline = row['deadline']
    if deadline is not None:
        deadline = min(deadline, wall + max(0, 600 - total))
    conn.execute('''UPDATE workflow_postmortems SET active_seconds=?,started_monotonic=?,
        deadline=? WHERE task_id=?''', (total, mono if running else None,
                                      0 if uncertain or total >= 600 else deadline, task_id))
    if uncertain or total >= 600:
        conn.execute("UPDATE workflow_incidents SET report_status='synthesis_failed' WHERE id=? AND report_status!='complete'", (row['incident_id'],))
        conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=? AND status!='done'", (task_id,))
    return conn.execute('SELECT * FROM workflow_postmortems WHERE task_id=?', (task_id,)).fetchone()


def deadlines(conn):
    from hermes_cli.kanban_db_connect import write_txn
    result = {}
    with write_txn(conn):
        for item in conn.execute('SELECT task_id FROM workflow_postmortems WHERE run_id IS NOT NULL').fetchall():
            row = settle(conn, item['task_id'])
            if row['deadline'] is not None:
                result[row['task_id']] = row['deadline']
    return result
