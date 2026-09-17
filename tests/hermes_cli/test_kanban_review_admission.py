"""Managed authority survives alternate task lifecycle writers."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect, write_txn


def enroll(conn, task, *, spec='a'*64):
    return state.enroll_review(conn, task, expected_status='ready', expected_run_id=None,
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest=spec, base_sha='b'*40, target_sha='c'*40,
        implementer_maker='openai', roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds': 0, 'recovery': 0, 'active_seconds': 0},
        compatibility={'cli': 1, 'gateway': 1, 'dashboard': 1}, decision='synthetic')


@pytest.mark.parametrize('change', ['board', 'schema', 'policy'])
def test_identity_or_unknown_schema_cannot_admit_reserved_work(tmp_path, monkeypatch, change):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='pinned identity', assignee='builder')
        attempt = enroll(conn, task)
        state.reserve_action(conn, task, category='preflight', expected_version=0)
        with write_txn(conn):
            if change == 'board':
                conn.execute("UPDATE workflow_board SET board_id='different-board'")
            elif change == 'schema':
                conn.execute('UPDATE workflow_board SET schema_version=99')
            else:
                conn.execute("UPDATE review_attempts SET policy_digest=?", ('0'*64,))
        assert kb.claim_task(conn, task) is None
        assert state.get_attempt(conn, task)['state'] == 'held'
        assert not kb.complete_task(conn, task, force=True)
    finally:
        conn.close()


@pytest.mark.parametrize('redaction_available', [True, False])
def test_managed_native_failure_redacts_before_durable_storage(tmp_path, monkeypatch, redaction_available):
    from hermes_cli import kanban_db_dispatch as dispatch
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    secret = 'sk-' + 'SyntheticSecretNeverPersist0123456789' * 2
    try:
        task = kb.create_task(conn, title='safe failure evidence', assignee='builder')
        enroll(conn, task)
        state.reserve_action(conn, task, category='preflight', expected_version=0)
        assert kb.claim_task(conn, task) is not None
        if not redaction_available:
            def unavailable(value):
                raise RuntimeError('redaction unavailable')
            monkeypatch.setattr(kb, 'redact_review_value', unavailable)
        dispatch._record_task_failure(conn, task, 'API_KEY=' + secret, outcome='spawn_failed',
                                      failure_limit=1, release_claim=True, end_run=True)
        for table in ('tasks', 'task_runs', 'task_events', 'workflow_incidents'):
            assert secret not in str([tuple(row) for row in conn.execute('SELECT * FROM ' + table)]), table
        assert secret not in kb.build_worker_context(conn, task)
        assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize('path', ['specify', 'decompose', 'delete', 'recreate', 'model'])
def test_managed_identity_cannot_be_rewritten_by_legacy_paths(tmp_path, monkeypatch, path):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='original ask', body='original acceptance', assignee='builder')
        attempt = enroll(conn, task)
        with write_txn(conn):
            state.hold(conn, attempt, 'review_exhausted')
            if path in {'specify', 'decompose'}:
                conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (task,))
        def mutation():
            if path == 'specify':
                return kb.specify_triage_task(conn, task, body='remove scope gate')
            if path == 'decompose':
                from hermes_cli.kanban_db_graph import decompose_triage_task
                return decompose_triage_task(conn, task, root_assignee='builder',
                                            children=[{'title': 'fresh budget', 'assignee': 'builder'}])
            if path == 'delete':
                return kb.delete_task(conn, task)
            if path == 'model':
                return kb.set_model_override(conn, task, 'claude-sonnet-4-5', 'anthropic')
            replacement = kb.create_task(conn, title='renamed ask', assignee='builder')
            return enroll(conn, replacement)
        with pytest.raises((ValueError, sqlite3.IntegrityError), match='managed|lineage|frozen'):
            mutation()
        assert kb.get_task(conn, task).body == 'original acceptance'
        assert state.get_attempt(conn, task)['id'] == attempt['id']
        assert state.get_attempt(conn, task)['state'] == 'held'
    finally:
        conn.close()
