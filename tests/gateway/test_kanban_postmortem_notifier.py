"""Postmortem notices reuse an authorized subscription, never a guessed route."""
import asyncio

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.gateway.test_kanban_changes_requested_notifier import RecordingAdapter, _runner, _run_one_tick


def test_postmortem_notice_uses_existing_subscription_once(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    with kbc.connect() as conn:
        owner = kb.create_task(conn, title='synthetic owner', assignee=None)
        kbn.add_notify_sub(conn, task_id=owner, platform='telegram', chat_id='fixture-chat',
                           thread_id='fixture-thread', delivery_mode='notify')
        with kb.write_txn(conn):
            kb._append_event(conn, owner, 'postmortem_report', {'incident_id': 'fixture-incident', 'attachment_id': 1})
    adapter = RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1
    assert adapter.sent[0]['chat_id'] == 'fixture-chat'
    assert 'postmortem' in adapter.sent[0]['text'].lower()
    assert 'not resolved' in adapter.sent[0]['text'].lower()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1


def test_acknowledgment_after_publication_suppresses_unclaimed_notice(tmp_path, monkeypatch):
    from hermes_cli.kanban_incident_operator import decide
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    with kbc.connect() as conn:
        owner = kb.create_task(conn, title='synthetic owner', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        incident = conn.execute('SELECT * FROM workflow_incidents').fetchone()
        kbn.add_notify_sub(conn, task_id=owner, platform='telegram', chat_id='fixture-chat', delivery_mode='notify')
        with kb.write_txn(conn):
            kb._append_event(conn, owner, 'postmortem_report', {'incident_id': incident['id'], 'attachment_id': 1})
        decide(conn, owner, {'operation': 'incident', 'incident_id': incident['id'], 'board_id': incident['board_id'],
                           'expected_state': 'open', 'state': 'acknowledged', 'clearance_events': []})
    adapter = RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert not adapter.sent, 'Acknowledgment must suppress a notice published but not yet admitted for delivery'
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))
    assert not adapter.sent
