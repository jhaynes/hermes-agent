"""Operator-only incident dispositions; reporting never clears implementation."""
import json
import os
import uuid

from hermes_cli.kanban_db_connect import write_txn


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_incident_dispositions (
        id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, receipt TEXT NOT NULL)''')


def decide(conn, task_id, receipt):
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise ValueError('workers cannot acknowledge or recover incidents')
    fields = {'operation', 'incident_id', 'board_id', 'expected_state', 'state', 'clearance_events'}
    if set(receipt) != fields or receipt['state'] not in {'acknowledged', 'recovered'}:
        raise ValueError('unsupported incident disposition')
    with write_txn(conn):
        incident = conn.execute('SELECT * FROM workflow_incidents WHERE id=? AND task_id=?',
                                (receipt['incident_id'], task_id)).fetchone()
        board = conn.execute('SELECT board_id FROM workflow_board').fetchone()[0]
        if (not incident or incident['board_id'] != board or receipt['board_id'] != board
                or incident['state'] != receipt['expected_state']):
            raise ValueError('incident board/owner/state CAS mismatch')
        sources = receipt['clearance_events']
        if not isinstance(sources, list) or len(sources) > 16 or any(type(s) is not int for s in sources):
            raise ValueError('clearance must cite bounded preserved events')
        if receipt['state'] == 'recovered' and not sources:
            raise ValueError('recovery requires clearance evidence')
        last_source = conn.execute('SELECT MAX(event_id) FROM workflow_incident_events WHERE incident_id=?', (incident['id'],)).fetchone()[0]
        for source in sources:
            event = conn.execute('SELECT kind FROM task_events WHERE id=? AND task_id=?', (source, task_id)).fetchone()
            if source <= last_source or not event or event['kind'] not in {'completed', 'unblocked'}:
                raise ValueError('clearance must cite a later owner completion or explicit unblock')
        conn.execute('INSERT INTO workflow_incident_dispositions VALUES(?,?,?)',
                     (str(uuid.uuid4()), incident['id'], json.dumps(receipt, sort_keys=True)))
        conn.execute('UPDATE workflow_incidents SET state=? WHERE id=?', (receipt['state'], incident['id']))
        from hermes_cli import kanban_db as kb
        kb._append_event(conn, task_id, 'incident_disposition', {'incident_id': incident['id'], 'state': receipt['state']})
    return {'incident_id': incident['id'], 'state': receipt['state']}
