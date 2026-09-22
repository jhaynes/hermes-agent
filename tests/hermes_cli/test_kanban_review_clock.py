"""Admission must settle the shared clock before another lane can start."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_review_cohort as cohort
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect


def reviewing(tmp_path, monkeypatch, active=7190):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    owner = kb.create_task(conn, title='clock contract', assignee='builder',
                           workspace_kind='dir', workspace_path=str(tmp_path))
    attempt = state.enroll_review(
        conn, owner, expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40,
        implementer_maker='openai', roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds': 0, 'recovery': 0, 'active_seconds': active},
        compatibility=writer_receipts(conn), decision='synthetic')
    now = [100.0]
    monkeypatch.setattr(state.time, 'monotonic', lambda: now[0])
    state.reserve_action(conn, owner, category='preflight', expected_version=0)
    run = kb.claim_task(conn, owner)
    cohort.record_runtime_route(conn, owner, run.current_run_id, provider='openai', model='gpt-5', isolated=False)
    assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
    lanes = {name: {'profile': 'reviewer', 'provider': 'anthropic',
                   'model': 'claude-sonnet-4-5', 'workspace': str(tmp_path / name)}
             for name in attempt['roster']}
    cohort.start_cohort(conn, owner, lanes=lanes,
                        expected_version=state.get_attempt(conn, owner)['version'])
    cards = [r[0] for r in conn.execute('SELECT task_id FROM review_members ORDER BY mandate')]
    return conn, owner, cards, now


@pytest.mark.parametrize('discontinuity', [False, True])
def test_claim_refuses_exhausted_or_uncertain_clock(tmp_path, monkeypatch, discontinuity):
    assert Path(state.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]
    conn, owner, cards, now = reviewing(tmp_path, monkeypatch)
    try:
        assert kb.claim_task(conn, cards[0]) is not None
        now[0] += -1 if discontinuity else 11
        assert kb.claim_task(conn, cards[1]) is None, 'No lane may start after a clock hold or exhaustion'
        attempt = state.get_attempt(conn, owner)
        assert attempt['state'] == 'held'
        assert attempt['hold_reason'] == ('clock_reconciliation_required' if discontinuity else 'active_time_exhausted')
        conn.close()
        conn = connect(tmp_path / 'board.db')
        assert kb.claim_task(conn, cards[2]) is None
        assert kb.get_task(conn, cards[1]).current_run_id is None
    finally:
        conn.close()


def test_expired_lane_completion_cannot_approve_or_accept_evidence(tmp_path, monkeypatch):
    conn, owner, cards, now = reviewing(tmp_path, monkeypatch)
    try:
        task = cards[0]
        run = kb.claim_task(conn, task)
        cohort.record_runtime_route(conn, task, run.current_run_id,
                                    provider='anthropic', model='claude-sonnet-4-5', isolated=True)
        attempt = state.get_attempt(conn, owner)
        member = conn.execute('SELECT * FROM review_members WHERE task_id=?', (task,)).fetchone()
        receipt = {key: attempt[key] for key in ('board_id', 'base_sha', 'target_sha', 'policy_digest', 'spec_digest')}
        receipt.update(attempt_id=attempt['id'], round_id=member['round_id'], mandate=member['mandate'],
                       task_id=task, run_id=run.current_run_id, verdict='approve', findings=[],
                       verification_run=[{'kind':'reasoned','reasoning':'synthetic clock unit test'}], prior_findings=[])
        now[0] += 11
        assert not kb.complete_task(conn, task, summary='Review receipt submitted', expected_run_id=run.current_run_id,
                                    metadata={'bounded_review': receipt}), 'Expired evidence must not become a valid lane'
        assert state.get_attempt(conn, owner)['state'] == 'held'
        assert conn.execute('SELECT receipt FROM review_members WHERE task_id=?', (task,)).fetchone()[0] is None
    finally:
        conn.close()


def test_dispatch_watchdog_quiesces_live_workers_on_union_exhaustion(tmp_path, monkeypatch):
    import subprocess
    import sys
    from hermes_cli import kanban_db_dispatch as dispatch

    conn, owner, cards, now = reviewing(tmp_path, monkeypatch)
    sleeper = tmp_path / 'worker.py'
    sleeper.write_text('import time\ntime.sleep(60)\n')
    processes = []
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda _: True)
    def spawn(task, workspace):
        process = subprocess.Popen([sys.executable, str(sleeper)])
        processes.append(process)
        return process.pid
    try:
        result = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=2, max_in_progress_per_profile=2)
        assert len(result.spawned) == 2
        now[0] += 11
        result = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=2, max_in_progress_per_profile=2)
        assert len(processes) == 2, 'Exhaustion cannot authorize replacement launches'
        assert state.get_attempt(conn, owner)['state'] == 'held'
        assert state.get_attempt(conn, owner)['active_seconds'] == pytest.approx(7201)
        for process in processes:
            assert process.wait(timeout=8) is not None, 'Supervisor must enforce union deadline, not merely count it'
        assert not conn.execute("SELECT 1 FROM review_actions WHERE state='running'").fetchone()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)
        conn.close()


def test_managed_deadline_overrides_terminal_worker_grace(tmp_path, monkeypatch):
    import subprocess
    import sys
    from hermes_cli import kanban_db_dispatch as dispatch

    conn, owner, cards, now = reviewing(tmp_path, monkeypatch)
    sleeper = tmp_path / 'terminal_worker.py'
    sleeper.write_text('import time\ntime.sleep(60)\n')
    processes = []
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda _: True)
    def spawn(task, workspace):
        process = subprocess.Popen([sys.executable, str(sleeper)])
        processes.append(process)
        return process.pid
    try:
        result = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        task = result.spawned[0][0]
        run = kb.get_task(conn, task).current_run_id
        cohort.record_runtime_route(conn, task, run, provider='anthropic', model='claude-sonnet-4-5', isolated=True)
        attempt = state.get_attempt(conn, task)
        member = conn.execute('SELECT * FROM review_members WHERE task_id=?',(task,)).fetchone()
        receipt = {key:attempt[key] for key in ('board_id','base_sha','target_sha','policy_digest','spec_digest')}
        receipt.update(attempt_id=attempt['id'], round_id=member['round_id'], mandate=member['mandate'],
                       task_id=task, run_id=run, verdict='approve', findings=[],
                       verification_run=[{'kind':'reasoned','reasoning':'synthetic terminal worker probe'}], prior_findings=[])
        assert kb.complete_task(conn, task, summary='Review receipt submitted', expected_run_id=run, metadata={'bounded_review':receipt})
        assert processes[0].poll() is None
        now[0] += 11
        dispatch.reap_terminal_workers(conn)
        assert processes[0].wait(timeout=5) is not None, 'Released claim cannot extend the managed deadline by reaper grace'
        assert state.get_attempt(conn, owner)['state'] == 'held'
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)
        conn.close()
