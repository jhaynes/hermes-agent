"""Diagnostic time is a cumulative physical-process budget, not per-run grace."""
import subprocess
import sys
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_postmortem as reports
from hermes_cli.kanban_db_connect import connect


def test_report_budget_charges_released_live_process_and_refuses_restart(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: p == 'diagnostic')
    conn = connect(tmp_path / 'board.db')
    owner = kb.create_task(conn, title='original failure', assignee=None)
    kb.block_task(conn, owner, kind='capability', reason='synthetic failure')
    children = []

    def spawn(task, workspace):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
        children.append(child)
        return child.pid

    try:
        dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        run = kb.get_task(conn, task).current_run_id
        # Simulate persisted consumption before a supervisor restart, not wall-time sleeps.
        with kb.write_txn(conn):
            conn.execute('UPDATE workflow_postmortems SET active_seconds=601')
            kb._end_run(conn, task, outcome='crashed', status='crashed')
        assert not kb.claim_task(conn, task), 'A released-but-live diagnostic may not overlap a replacement'
        conn.close()
        conn = connect(tmp_path / 'board.db')
        def broken_publisher(*args):
            raise OSError('synthetic publication failure')
        monkeypatch.setattr(reports, 'publish_reports', broken_publisher)
        dispatch.dispatch_once(conn, max_spawn=0)
        assert children[0].poll() is not None, 'The absolute diagnostic budget must override ordinary terminal grace'
        assert conn.execute('SELECT report_status FROM workflow_incidents').fetchone()[0] == 'synthesis_failed'
        assert not kb.claim_task(conn, task)
        assert kb.get_task(conn, owner).status == 'blocked'
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        conn.close()


def test_expired_reporter_request_and_uncertain_restart_are_refused(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytest
    from hermes_cli.kanban_review_worker import before_model_request
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    with connect(db) as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='failure')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        run = kb.claim_task(conn, task).current_run_id
        monkeypatch.setenv('HERMES_KANBAN_TASK', task)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
        with kb.write_txn(conn):
            conn.execute("UPDATE workflow_postmortems SET boot_id='unknown-boot'")
        with pytest.raises((TimeoutError, InterruptedError)):
            before_model_request(SimpleNamespace(max_iterations=5), {})
        assert conn.execute('SELECT report_status FROM workflow_incidents').fetchone()[0] == 'synthesis_failed'
