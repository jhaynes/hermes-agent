"""Diagnostic counters travel as history, never copied dispatch authority."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_postmortem as reports
from hermes_cli import kanban_transfer as transfer
from hermes_cli.kanban_db_connect import connect, connect_closing


def test_diagnostic_clone_preserves_spent_run_but_cannot_resume(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key, raising=False)
    kb.create_board('source')
    with connect_closing(board='source') as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        worker = kb.claim_task(conn, task)
        with kb.write_txn(conn):
            kb._end_run(conn, task, outcome='spawn_failed')
            conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL WHERE id=?", (task,))
    archive = transfer.export_board('source', str(tmp_path / 'snapshot'))['archive']
    transfer.import_board(archive, slug='copy')
    with connect_closing(board='copy') as conn:
        assert conn.execute('SELECT runs_started FROM workflow_postmortems').fetchone()[0] == 1
        assert kb.claim_task(conn, task) is None, 'A writable clone cannot spend the originating incident retry'
        assert kb.get_task(conn, task).status == 'blocked'
    with connect_closing(board='source') as conn:
        assert kb.claim_task(conn, task) is not None


def test_unknown_workflow_version_refuses_diagnostic_claim(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        with kb.write_txn(conn):
            conn.execute('UPDATE workflow_board SET schema_version=99')
        assert kb.claim_task(conn, task) is None
        assert conn.execute('SELECT runs_started FROM workflow_postmortems').fetchone()[0] == 0
    import pytest
    from hermes_cli import kanban_db_connect
    monkeypatch.setattr(kanban_db_connect, '_INITIALIZED_PATHS', set())
    with pytest.raises(ValueError, match='unsupported workflow schema'):
        connect(tmp_path / 'board.db')


def test_additive_diagnostic_migration_retains_history(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = tmp_path / 'board.db'
    with connect(db) as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        reports.queue_reports(conn)
        incident = conn.execute('SELECT id FROM workflow_incidents').fetchone()[0]
        with kb.write_txn(conn):
            conn.execute('UPDATE workflow_postmortems SET runs_started=1,active_seconds=42')
            conn.execute('ALTER TABLE workflow_postmortems DROP COLUMN failure_kind')
    from hermes_cli import kanban_db_connect
    monkeypatch.setattr(kanban_db_connect, '_INITIALIZED_PATHS', set())
    for _ in range(2):
        with connect(db) as conn:
            row = conn.execute('SELECT * FROM workflow_postmortems').fetchone()
            assert row['incident_id'] == incident and row['runs_started'] == 1 and row['active_seconds'] == 42
            assert row['failure_kind'] is None
            assert kb.claim_task(conn, row['task_id']) is None, 'Historical retry without failure provenance must stay held'
