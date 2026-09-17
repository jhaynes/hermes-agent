"""Durable workflow capture through the real block lifecycle."""
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def test_block_capture_is_durable_without_observers(tmp_path, monkeypatch):
    conn = connect(tmp_path / 'board.db')
    try:
        monkeypatch.setattr(kb, '_fire_task_hook', lambda *a, **kw: None)
        task = kb.create_task(conn, title='isolated wait', assignee='builder')
        run = kb.claim_task(conn, task)
        assert run is not None
        assert kb.block_task(conn, task, reason='waiting for owner', kind='needs_input', expected_run_id=run.current_run_id)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'workflow_incidents' in tables, 'Every actual block needs durable incident capture'
        incident = dict(conn.execute('SELECT * FROM workflow_incidents WHERE task_id=?', (task,)).fetchone())
        assert incident['classification'] == 'expected_wait'
        assert incident['report_status'] == 'complete'
        assert incident['state'] == 'open'
        assert incident['run_id'] == run.current_run_id
    finally:
        conn.close()


def test_dispatch_reconciles_missed_capture_once_and_links_recurrence(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_workflow_incidents as incidents
    from hermes_cli.kanban_db_connect import write_txn

    path = tmp_path / 'board.db'
    conn = connect(path)
    task = kb.create_task(conn, title='synthetic crash', assignee=None)
    run = kb.claim_task(conn, task)
    assert run is not None
    # Simulate an older writer/missed callback; durable event is authoritative.
    with monkeypatch.context() as scoped:
        scoped.setattr(incidents, 'capture_event', lambda *a: None)
        with write_txn(conn):
            kb._append_event(conn, task, 'crashed', {'error': 'synthetic-secret-do-not-store'}, run_id=run.current_run_id)
            kb._append_event(conn, task, 'gave_up', {}, run_id=run.current_run_id)
        assert kb.block_task(conn, task, kind='transient', reason='crash', expected_run_id=run.current_run_id)
    conn.close()
    conn = connect(path)
    dispatch.dispatch_once(conn, max_spawn=1)
    rows = conn.execute('SELECT * FROM workflow_incidents').fetchall()
    assert len(rows) == 1, 'A dispatcher tick must recover durable events after a missed capture'
    first = dict(rows[0])
    assert first['classification'] == 'failure'
    assert 'synthetic-secret-do-not-store' not in str(first)
    dispatch.dispatch_once(conn, max_spawn=1)
    assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0] == 1
    assert kb.unblock_task(conn, task)
    second_run = kb.claim_task(conn, task)
    assert second_run is not None
    with write_txn(conn):
        kb._append_event(conn, task, 'crashed', {}, run_id=second_run.current_run_id)
    second = dict(conn.execute('SELECT * FROM workflow_incidents ORDER BY rowid DESC LIMIT 1').fetchone())
    assert second['id'] != first['id']
    assert second['prior_incident_id'] == first['id']
    dispatch._record_task_failure(conn,task,'synthetic crash',outcome='crashed',failure_limit=100,
                                  release_claim=True,end_run=True)
    dispatch._record_task_failure(conn,task,'synthetic crash',outcome='crashed',force_trip=True)
    assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0]==2, 'Native runless gave_up must retain the preceding crash episode'
    conn.close()
