"""Bounded review enrollment and admission contracts on temporary boards."""
import pytest
from tests.hermes_cli.review_readiness_helpers import writer_receipts
import importlib.util
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def test_enrollment_pins_lineage_and_refuses_legacy_approval(tmp_path, monkeypatch):
    assert importlib.util.find_spec('hermes_cli.kanban_review_state'), 'Managed review enrollment is unavailable'
    from hermes_cli import kanban_review_state as workflow
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='approved ask', assignee='builder')
        enroll = getattr(workflow, 'enroll_review', None)
        assert callable(enroll), 'Operator enrollment must bind an attempt before dispatch'
        board_id = conn.execute('SELECT board_id FROM workflow_board').fetchone()[0]
        monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
        attempt = enroll(conn, task, expected_status='ready', expected_run_id=None, expected_assignee='builder',
                         board_id=board_id, spec_digest='a'*64, base_sha='b'*40,
                         target_sha='c'*40, implementer_maker='openai',
                         roster=['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
                         consumed={'rounds':0,'recovery':0,'active_seconds':0},
                         compatibility=writer_receipts(conn), decision='synthetic operator decision')
        assert attempt['state'] == 'preflight'
        with pytest.raises(ValueError, match='already enrolled'):
            enroll(conn, task, expected_status='ready', expected_run_id=None, expected_assignee='builder',
                   board_id=board_id, spec_digest='d'*64, base_sha='b'*40,
                   target_sha='c'*40, implementer_maker='openai', roster=attempt['roster'],
                   consumed={'rounds':0,'recovery':0,'active_seconds':0},
                   compatibility=writer_receipts(conn), decision='reset attempt')
        assert kb.complete_task(conn, task, summary='ordinary approval') is False
        assert kb.get_task(conn, task).status == 'ready'
    finally:
        conn.close()


def test_exhausted_migration_cannot_dispatch_or_downgrade(tmp_path, monkeypatch):
    import sqlite3
    from hermes_cli.kanban_review_state import enroll_review
    from hermes_cli import kanban_db_dispatch as dispatch
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    conn = connect(path)
    task = kb.create_task(conn, title='exhausted implementation', assignee='builder')
    attempt = enroll_review(conn, task, expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
        consumed={'rounds':3,'recovery':2,'active_seconds':90},
        compatibility=writer_receipts(conn), decision='preserve consumed history')
    assert attempt['state'] == 'held'
    assert kb.claim_task(conn, task) is None, 'Managed hold must override ready column/manual claim'
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda _: True)
    spawns = []
    dispatch.dispatch_once(conn, spawn_fn=lambda *a: spawns.append(a), max_spawn=1)
    assert spawns == []
    assert not kb.complete_task(conn, task, force=True)
    # Old runtime fixture has no registered workflow capability, even if it can
    # write ordinary legacy rows. Persisted guard must survive code downgrade.
    old = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match='workflow|managed'):
        old.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
    old.close()
    conn.close()


def test_failed_preflight_cannot_native_respawn_or_reset_shared_recovery(tmp_path, monkeypatch):
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    conn = connect(path)
    task = kb.create_task(conn, title='finite preflight', assignee='builder')
    state.enroll_review(conn, task, expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
        consumed={'rounds':0,'recovery':0,'active_seconds':0},
        compatibility=writer_receipts(conn), decision='synthetic')
    reserve = getattr(state, 'reserve_action', None)
    assert callable(reserve), 'Managed workers require a durable action reservation'
    for retry in range(3):
        attempt = state.get_attempt(conn, task)
        action = reserve(conn, task, category='preflight', expected_version=attempt['version'], recovery=retry > 0)
        assert action is not None
        run = kb.claim_task(conn, task)
        assert run is not None
        assert run.max_runtime_seconds <= 7200
        assert kb.claim_task(conn, task) is None
        _record_task_failure(conn, task, 'synthetic spawn failure', outcome='spawn_failed',
                             failure_limit=100, release_claim=True, end_run=True)
        assert kb.claim_task(conn, task) is None, 'Native failure retries must not multiply reservations'
        conn.close()
        conn = connect(path)
    attempt = state.get_attempt(conn, task)
    assert attempt['recovery_used'] == 2
    assert reserve(conn, task, category='preflight', expected_version=attempt['version'], recovery=True) is None
    assert state.get_attempt(conn, task)['state'] == 'held'
    assert kb.claim_task(conn, task) is None
    conn.close()


def test_running_time_is_charged_but_queue_wait_is_not(tmp_path, monkeypatch):
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    task = kb.create_task(conn, title='clock', assignee='builder')
    state.enroll_review(conn, task, expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
        consumed={'rounds':0,'recovery':0,'active_seconds':10},
        compatibility=writer_receipts(conn), decision='synthetic')
    now = [100.0]
    monkeypatch.setattr(state.time, 'monotonic', lambda: now[0])
    state.reserve_action(conn, task, category='preflight', expected_version=0)
    now[0] += 500
    run = kb.claim_task(conn, task)
    assert run is not None
    now[0] += 12
    _record_task_failure(conn, task, 'failure', outcome='spawn_failed', failure_limit=100,
                         release_claim=True, end_run=True)
    assert state.get_attempt(conn, task)['active_seconds'] == pytest.approx(22), 'Persist execution time, not queue time'
    now[0] += 800
    assert state.get_attempt(conn, task)['active_seconds'] == pytest.approx(22)
    conn.close()
