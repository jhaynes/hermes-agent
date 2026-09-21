"""Managed authority survives alternate task lifecycle writers."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect, write_txn


def enroll(conn, task, *, spec='a'*64):
    return state.enroll_review(conn, task, expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest=spec, base_sha='b'*40, target_sha='c'*40,
        implementer_maker='openai', roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds': 0, 'recovery': 0, 'active_seconds': 0},
        compatibility=writer_receipts(conn), decision='synthetic')


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


def test_handoff_cannot_pause_or_start_cohort_while_worker_is_live(tmp_path, monkeypatch):
    import subprocess
    import sys
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_review_cohort as cohort
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda _: True)
    conn = connect(tmp_path / 'board.db')
    sleeper = tmp_path / 'worker.py'
    sleeper.write_text('import time\ntime.sleep(60)\n')
    processes = []
    clock = [100.0]
    monkeypatch.setattr(state.time, 'monotonic', lambda: clock[0])
    def spawn(task, workspace):
        process = subprocess.Popen([sys.executable, str(sleeper)])
        processes.append(process)
        return process.pid
    try:
        task = kb.create_task(conn, title='quiescent handoff', assignee='builder', workspace_kind='dir', workspace_path=str(tmp_path))
        attempt = enroll(conn, task)
        state.reserve_action(conn, task, category='preflight', expected_version=0)
        assert len(dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1).spawned) == 1
        run_id = kb.get_task(conn, task).current_run_id
        cohort.record_runtime_route(conn, task, run_id, provider='openai', model='gpt-5', isolated=False)
        assert kb.request_review(conn, task, expected_run_id=run_id)
        lanes = {name: {'profile': 'reviewer', 'provider': 'anthropic', 'model': 'claude-sonnet-4-5',
                        'workspace': str(tmp_path / name)} for name in attempt['roster']}
        assert processes[0].poll() is None
        clock[0] += 11
        dispatch.enforce_max_runtime(conn)
        assert state.get_attempt(conn, task)['active_seconds'] == pytest.approx(11), 'A released but live worker is not a clock pause'
        with pytest.raises(ValueError, match='quiescent'):
            cohort.start_cohort(conn, task, lanes=lanes, expected_version=1)
        monkeypatch.setattr(dispatch, 'TERMINAL_WORKER_REAP_GRACE_SECONDS', 0)
        dispatch.reap_terminal_workers(conn)
        assert processes[0].wait(timeout=8) is not None
        assert cohort.start_cohort(conn, task, lanes=lanes, expected_version=1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)
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


@pytest.mark.parametrize('source', ['creator', 'worker'])
def test_implementation_owner_cannot_recreate_unmanaged_work(tmp_path, monkeypatch, source):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        owner = kb.create_task(conn, title='approved ask', assignee='builder')
        attempt = enroll(conn, owner)
        with write_txn(conn):
            state.hold(conn, attempt, 'review_exhausted')
        kwargs = {'creator_task_id': owner} if source == 'creator' else {}
        if source == 'worker':
            monkeypatch.setenv('HERMES_KANBAN_TASK', owner)
            monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
        with pytest.raises(ValueError, match='managed.*creation|nested'):
            kb.create_task(conn, title='renamed replacement', assignee='builder', **kwargs)
        assert len(kb.list_tasks(conn)) == 1
        assert state.get_attempt(conn, owner)['id'] == attempt['id']
        monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
        legacy = kb.create_task(conn, title='independent legacy ask', assignee='builder')
        assert kb.claim_task(conn, legacy) is not None
    finally:
        conn.close()
