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
    "review_exhausted", "recovery_exhausted", "active_time_exhausted",
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
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_incidents (
            id TEXT PRIMARY KEY, board_id TEXT NOT NULL, task_id TEXT NOT NULL,
            run_id INTEGER, episode_key TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL, prior_incident_id TEXT,
            classification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
            report_status TEXT NOT NULL, lesson_status TEXT NOT NULL DEFAULT 'proposed',
            source_events TEXT NOT NULL, report TEXT, created_at INTEGER NOT NULL)""")
        from hermes_cli.kanban_postmortem import initialize as initialize_postmortems
        initialize_postmortems(conn)
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
    if kind=='gave_up' and run_id is None:
        cause=conn.execute("""SELECT kind,run_id FROM task_events WHERE task_id=? AND id<?
            AND kind IN ('claimed','crashed','timed_out','spawn_failed') ORDER BY id DESC LIMIT 1""",
            (task_id,event_id)).fetchone()
        if cause and cause['kind']==(payload or {}).get('trigger_outcome'):
            run_id=cause['run_id']
    board = conn.execute("SELECT board_id FROM workflow_board WHERE singleton=1").fetchone()[0]
    # A terminal run's crash/gave_up/block cascade is one causal episode.
    # Runless transitions are independent unless replaying that exact event.
    episode = f"{board}:{task_id}:" + (f"run:{run_id}" if run_id is not None else f"event:{event_id}")
    prior = conn.execute("SELECT * FROM workflow_incidents WHERE episode_key=?", (episode,)).fetchone()
    if prior:
        events = json.loads(prior['source_events'])
        if event_id not in events:
            events.append(event_id)
            conn.execute("UPDATE workflow_incidents SET source_events=? WHERE id=?", (json.dumps(events), prior['id']))
        return
    typed = (payload or {}).get('kind')
    expected = kind == 'dependency_wait' or (kind == 'blocked' and typed in {'dependency', 'needs_input'})
    classification = 'expected_wait' if expected else 'failure'
    fingerprint = f"{task_id}:{classification}:{kind}"
    previous = conn.execute(
        "SELECT id FROM workflow_incidents WHERE fingerprint=? ORDER BY rowid DESC LIMIT 1", (fingerprint,),
    ).fetchone()
    # Never persist arbitrary error text, paths, commands, URLs or log bodies.
    report = json.dumps({
        'schema': 1, 'reason': typed if typed in {'dependency', 'needs_input'} else 'dependency',
        'source_event': event_id, 'next_action': 'await_owner',
    }) if expected else None
    conn.execute(
        """INSERT INTO workflow_incidents
        (id,board_id,task_id,run_id,episode_key,fingerprint,prior_incident_id,
         classification,report_status,source_events,report,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), board, task_id, run_id, episode, fingerprint,
         previous[0] if previous else None, classification,
         'complete' if expected else 'queued', json.dumps([event_id]), report, created_at),
    )
