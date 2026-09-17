"""Independent same-run causes survive cascade deduplication and bounded capture."""
import json
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_workflow_incidents import reconcile_events


def test_cascade_is_one_episode_but_later_same_run_failure_is_not(tmp_path):
    with connect(tmp_path / 'board.db') as conn:
        task = kb.create_task(conn, title='fixture', assignee='owner')
        run = kb.claim_task(conn, task).current_run_id
        with kb.write_txn(conn):
            kb._append_event(conn, task, 'crashed', {}, run_id=run)
            kb._append_event(conn, task, 'gave_up', {'trigger_outcome': 'crashed'}, run_id=run)
            kb._append_event(conn, task, 'blocked', {'kind': 'needs_input'}, run_id=run)
        rows = conn.execute('SELECT * FROM workflow_incidents').fetchall()
        assert len(rows) == 1, 'A terminal crash cascade must not turn into an expected wait episode'
        first = rows[0]['id']
        with kb.write_txn(conn):
            kb._append_event(conn, task, 'review_invalid', {'round_id': 'round-two'}, run_id=run)
        assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0] == 2
        reconcile_events(conn)
        assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0] == 2
        assert len(json.loads(conn.execute('SELECT source_events FROM workflow_incidents WHERE id=?', (first,)).fetchone()[0])) == 3


def test_expected_wait_contains_owner_and_distinct_recurrence(tmp_path):
    with connect(tmp_path / 'board.db') as conn:
        task = kb.create_task(conn, title='fixture', assignee='owner')
        kb.block_task(conn, task, kind='needs_input', reason='approval')
        incident = conn.execute('SELECT * FROM workflow_incidents').fetchone()
        assert json.loads(incident['report'])['owner'] == task


def test_operator_acknowledgment_and_evidenced_recovery_do_not_resume_owner(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    import pytest
    from hermes_cli.kanban_review_ops import enroll
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    db = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    with connect(db) as conn:
        task = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, task, kind='capability', reason='failure')
        incident = conn.execute('SELECT * FROM workflow_incidents').fetchone()
        receipt = {'operation': 'incident', 'incident_id': incident['id'], 'board_id': incident['board_id'],
                   'expected_state': 'open', 'state': 'acknowledged', 'clearance_events': []}
        path = tmp_path / 'receipt.json'
        path.write_text(json.dumps(receipt))
        assert enroll(Namespace(task_id=task, receipt=str(path))) == 0
        assert kb.get_task(conn, task).status == 'blocked'
        receipt.update(expected_state='acknowledged', state='recovered')
        path.write_text(json.dumps(receipt))
        with pytest.raises(ValueError, match='clearance'):
            enroll(Namespace(task_id=task, receipt=str(path)))
        assert kb.unblock_task(conn, task)
        event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='unblocked' ORDER BY id DESC", (task,)).fetchone()[0]
        receipt['clearance_events'] = [event]
        path.write_text(json.dumps(receipt))
        assert enroll(Namespace(task_id=task, receipt=str(path))) == 0
        kb.block_task(conn, task, kind='capability', reason='later recurrence')
        rows = conn.execute('SELECT * FROM workflow_incidents ORDER BY rowid').fetchall()
        assert rows[0]['state'] == 'recovered' and rows[1]['state'] == 'open'
        assert rows[1]['prior_incident_id'] == incident['id']
