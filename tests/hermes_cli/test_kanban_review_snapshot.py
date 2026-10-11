"""Approval of a frozen cohort never approves a later owner snapshot."""
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from tests.hermes_cli.review_evidence_helpers import scope_evidence
from hermes_cli import kanban_review_state as state
from hermes_cli import kanban_review_cohort as cohort
from hermes_cli.kanban_db_connect import connect


@pytest.mark.parametrize('mutation', ['none', 'dirty', 'new_commit'])
def test_completion_checks_actual_approved_snapshot(tmp_path, monkeypatch, mutation):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    source = repo / 'feature.txt'
    source.write_text('approved behavior\n')
    git('add', '.')
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'frozen')
    sha = git('rev-parse', 'HEAD')
    conn = connect(tmp_path / 'board.db')
    try:
        owner = kb.create_task(conn, title='frozen ask', assignee='builder', workspace_kind='dir', workspace_path=str(repo))
        attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None, expected_assignee='builder',
            board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0], spec_digest='a'*64,
            base_sha=sha, target_sha=sha, implementer_maker='openai', roster=sorted(state.REQUIRED_LANES),
            consumed={'rounds': 0, 'recovery': 0, 'active_seconds': 0},
            compatibility=writer_receipts(conn), decision='synthetic')
        state.reserve_action(conn, owner, category='preflight', expected_version=0)
        run = kb.claim_task(conn, owner)
        cohort.record_runtime_route(conn, owner, run.current_run_id, provider='openai', model='gpt-5', isolated=False)
        assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
        lanes = {name: {'profile': 'reviewer', 'provider': 'anthropic', 'model': 'claude-sonnet-4-5',
                        'workspace': str(tmp_path / name)} for name in attempt['roster']}
        cohort.start_cohort(conn, owner, lanes=lanes, expected_version=1)
        for member in conn.execute('SELECT * FROM review_members').fetchall():
            task = member['task_id']
            run = kb.claim_task(conn, task)
            cohort.record_runtime_route(conn, task, run.current_run_id,
                                        provider='anthropic', model='claude-sonnet-4-5', isolated=True)
            receipt = {key: attempt[key] for key in ('board_id', 'base_sha', 'target_sha', 'policy_digest', 'spec_digest')}
            receipt.update(attempt_id=attempt['id'], round_id=member['round_id'], mandate=member['mandate'],
                           task_id=task, run_id=run.current_run_id, verdict='approve', findings=[],
                           verification_run=[{'kind':'reasoned','reasoning':'synthetic completion-gate unit test'}], prior_findings=[], scope=scope_evidence())
            assert kb.complete_task(conn, task, summary='Review receipt submitted', expected_run_id=run.current_run_id, metadata={'bounded_review': receipt})
        assert state.get_attempt(conn, owner)['state'] == 'approved'
        if mutation != 'none':
            source.write_text('unreviewed extra behavior\n')
            if mutation == 'new_commit':
                git('add', '.')
                git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'unreviewed')
        assert kb.complete_task(conn, owner, summary='Attempted completion', force=True) is (mutation == 'none'), 'Force cannot inherit stale approval'
        if mutation != 'none':
            assert state.get_attempt(conn, owner)['state'] == 'held'
            assert source.read_text() == 'unreviewed extra behavior\n', 'Hold must preserve unique work'
    finally:
        conn.close()
