"""Board-local incident capture. Evidence identifiers are safe even without redaction.

Capture is part of the lifecycle transaction, not a best-effort observer. A report
is a separate outcome; producing one never resolves the original incident.
"""
from __future__ import annotations

import json
import uuid

from hermes_cli.kanban_db_connect import write_txn


TRIGGERS = frozenset({
    "blocked", "dependency_wait", "block_loop_detected", "crashed",
    "timed_out", "spawn_failed", "gave_up", "review_invalid",
    "review_exhausted", "recovery_exhausted", "active_time_exhausted", "queue_stalled",
})


def initialize(conn):
    """Additive, transactional initialization, retaining board identity on restore."""
    with write_txn(conn, allow_nested=True):
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_board (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            board_id TEXT NOT NULL UNIQUE, schema_version INTEGER NOT NULL,
            event_cursor INTEGER NOT NULL DEFAULT 0)""")
        conn.execute(
            "INSERT OR IGNORE INTO workflow_board(singleton,board_id,schema_version) VALUES(1,?,1)",
            (str(uuid.uuid4()),),
        )
        if conn.execute('SELECT schema_version FROM workflow_board WHERE singleton=1').fetchone()[0] != 1:
            raise ValueError('unsupported workflow schema; owning runtime required')
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_incidents (
            id TEXT PRIMARY KEY, board_id TEXT NOT NULL, task_id TEXT NOT NULL,
            run_id INTEGER, episode_key TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL, prior_incident_id TEXT,
            classification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
            report_status TEXT NOT NULL, lesson_status TEXT NOT NULL DEFAULT 'proposed',
            source_events TEXT NOT NULL, report TEXT, created_at INTEGER NOT NULL)""")
        from hermes_cli.kanban_postmortem import initialize as initialize_postmortems
        conn.execute('''CREATE TABLE IF NOT EXISTS workflow_incident_events (
            event_id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL)''')
        conn.execute('''INSERT OR IGNORE INTO workflow_incident_events
            SELECT CAST(j.value AS INTEGER), i.id FROM workflow_incidents i, json_each(i.source_events) j''')
        initialize_postmortems(conn)
        from hermes_cli.kanban_incident_operator import initialize as initialize_dispositions
        initialize_dispositions(conn)
        from hermes_cli.kanban_workflow_lessons import initialize as initialize_lessons
        initialize_lessons(conn)


def reconcile_events(conn, batch_size=256):
    """Recover missed capture in bounded batches; cursor and captures commit together."""
    with write_txn(conn):
        cursor = conn.execute('SELECT event_cursor FROM workflow_board WHERE singleton=1').fetchone()[0]
        rows = conn.execute('SELECT * FROM task_events WHERE id>? ORDER BY id LIMIT ?', (cursor, batch_size)).fetchall()
        for row in rows:
            try:
                payload = json.loads(row['payload'] or '{}')
            except (ValueError, TypeError):
                payload = {}
            capture_event(conn, row['id'], row['task_id'], row['run_id'], row['kind'],
                          payload if isinstance(payload, dict) else {}, row['created_at'])
        if rows:
            conn.execute('UPDATE workflow_board SET event_cursor=? WHERE singleton=1', (rows[-1]['id'],))


def capture_event(conn, event_id, task_id, run_id, kind, payload, created_at):
    if kind not in TRIGGERS:
        return
    from hermes_cli.kanban_postmortem import is_diagnostic
    if is_diagnostic(conn, task_id):
        return
    if conn.execute('SELECT 1 FROM workflow_incident_events WHERE event_id=?', (event_id,)).fetchone():
        return
    if kind=='gave_up' and run_id is None:
        cause=conn.execute("""SELECT kind,run_id FROM task_events WHERE task_id=? AND id<?
            AND kind IN ('claimed','crashed','timed_out','spawn_failed') ORDER BY id DESC LIMIT 1""",
            (task_id,event_id)).fetchone()
        if cause and cause['kind']==(payload or {}).get('trigger_outcome'):
            run_id=cause['run_id']
    board = conn.execute("SELECT board_id FROM workflow_board WHERE singleton=1").fetchone()[0]
    typed = (payload or {}).get('kind')
    expected = kind == 'dependency_wait' or (kind == 'blocked' and typed in {'dependency', 'needs_input'})
    classification = 'expected_wait' if expected else 'failure'
    if kind == 'queue_stalled':
        classification = 'stalled_queue'
    # Equal run/classification does not prove two failures share a cause.
    cause = None
    if kind in {'gave_up', 'blocked', 'crashed', 'timed_out', 'spawn_failed'}:
        cause = conn.execute('''SELECT e.kind, i.* FROM task_events e
            JOIN workflow_incident_events s ON s.event_id=e.id
            JOIN workflow_incidents i ON i.id=s.incident_id
            WHERE e.task_id=? AND e.id<? AND i.run_id IS ?
            ORDER BY e.id DESC LIMIT 1''', (task_id, event_id, run_id)).fetchone()
        if (cause and cause['classification'] == 'failure'
                and cause['kind'] in {'crashed', 'timed_out', 'spawn_failed', 'gave_up'}
                and (kind in {'gave_up', 'blocked'} or (run_id is not None and cause['kind'] == kind))):
            classification, expected = 'failure', False
        else:
            cause = None
    episode = f"{board}:{task_id}:event:{event_id}"
    classified_episode = f'{episode}:{classification}'
    prior = cause
    if prior:
        conn.execute('INSERT INTO workflow_incident_events VALUES(?,?)', (event_id, prior['id']))
        events = json.loads(prior['source_events'])
        if event_id not in events and len(events) < 32:
            events.append(event_id)
            conn.execute("UPDATE workflow_incidents SET source_events=? WHERE id=?", (json.dumps(events), prior['id']))
        return
    family = 'blocked' if kind == 'block_loop_detected' else kind
    fingerprint = f"{task_id}:{classification}:{family}"
    previous = conn.execute(
        "SELECT id FROM workflow_incidents WHERE fingerprint=? ORDER BY rowid DESC LIMIT 1", (fingerprint,),
    ).fetchone()
    # Never persist arbitrary error text, paths, commands, URLs or log bodies.
    report = json.dumps({
        'schema': 1, 'reason': typed if typed in {'dependency', 'needs_input'} else 'dependency',
        'source_event': event_id, 'next_action': 'await_owner',
        'owner': task_id,
    }) if expected else None
    incident_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO workflow_incidents
        (id,board_id,task_id,run_id,episode_key,fingerprint,prior_incident_id,
         classification,report_status,source_events,report,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (incident_id, board, task_id, run_id, classified_episode, fingerprint,
         previous[0] if previous else None, classification,
         'complete' if expected else 'queued', json.dumps([event_id]), report, created_at),
    )
    conn.execute('INSERT INTO workflow_incident_events VALUES(?,?)', (event_id, incident_id))
    if previous:
        from hermes_cli.kanban_workflow_lessons import reevaluate_recurrence
        reevaluate_recurrence(conn, incident_id, fingerprint)


def diagnose_unowned_queue(conn, stale_timeout_seconds):
    """Reuse the dispatcher's stale threshold; no ownership or capacity changes."""
    if stale_timeout_seconds <= 0:
        return
    import time
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_postmortem import is_diagnostic
    from hermes_cli.kanban_db_dispatch import _profile_exists_fn
    profile_exists = _profile_exists_fn()
    with write_txn(conn):
        rows = conn.execute('''SELECT t.id,t.assignee, MAX(e.id) AS episode, MAX(e.created_at) AS last_at FROM tasks t
            JOIN task_events e ON e.task_id=t.id AND e.kind IN
                ('created','promoted','assigned','unblocked','status','claimed','reclaimed',
                 'stale','crashed','timed_out','spawn_failed','review_reopened','changes_requested')
            WHERE t.status='ready' AND t.current_run_id IS NULL
            GROUP BY t.id HAVING last_at < ? AND MAX(e.id) > COALESCE(
                (SELECT MAX(q.id) FROM task_events q WHERE q.task_id=t.id AND q.kind='queue_stalled'),0)
            ORDER BY last_at''',
            (time.time() - stale_timeout_seconds,))
        captured = 0
        for row in rows:
            if row['assignee'] and (profile_exists is None or profile_exists(row['assignee'])):
                continue
            if not is_diagnostic(conn, row['id']):
                kb._append_event(conn, row['id'], 'queue_stalled', {'next_action': 'assign_owner'})
                captured += 1
                if captured == 16:
                    break
