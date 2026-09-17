"""Whole-cohort completion uses actual lane-card lifecycle, never verdict counts."""
import importlib.util
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect


@pytest.mark.parametrize('scope_changes', [False, True])
def test_third_cohort_requires_scope_and_never_grants_fourth_round(tmp_path, monkeypatch, scope_changes):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    owner = kb.create_task(conn, title='approved ask', assignee='builder', workspace_kind='dir', workspace_path=str(tmp_path))
    attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None,
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
        consumed={'rounds':2,'recovery':0,'active_seconds':0},
        compatibility={'cli':1,'gateway':1,'dashboard':1}, decision='conservative adoption')
    state.reserve_action(conn, owner, category='preflight', expected_version=0)
    run = kb.claim_task(conn, owner)
    assert run is not None
    assert kb.request_review(conn, owner, summary='preflight complete', expected_run_id=run.current_run_id)
    assert importlib.util.find_spec('hermes_cli.kanban_review_cohort'), 'Whole-cohort routing is missing'
    from hermes_cli import kanban_review_cohort as cohort
    lanes = {name: {'profile':'reviewer','model':'claude-sonnet-4-5','provider':'anthropic',
                    'workspace':str(tmp_path / name)} for name in attempt['roster']}
    for lane in lanes.values():
        from pathlib import Path
        Path(lane['workspace']).mkdir()
    round_id = cohort.start_cohort(conn, owner, lanes=lanes, expected_version=1)
    cards = conn.execute('SELECT * FROM review_members WHERE round_id=? ORDER BY mandate', (round_id,)).fetchall()
    assert {r['mandate'] for r in cards} == set(attempt['roster'])
    for card in sorted(cards, key=lambda c: c['mandate'] == 'scope'):
        lane_run = kb.claim_task(conn, card['task_id'])
        assert lane_run is not None
        cohort.record_runtime_route(conn, card['task_id'], lane_run.current_run_id,
                                    provider='anthropic', model='claude-sonnet-4-5', isolated=True)
        findings = [{'severity':'high','evidence':'executed synthetic probe','required_change':'remove unrelated behavior'}] if scope_changes and card['mandate']=='scope' else []
        receipt = {'attempt_id':attempt['id'],'board_id':attempt['board_id'],'round_id':round_id,
                   'task_id':card['task_id'],'run_id':lane_run.current_run_id,
                   'base_sha':attempt['base_sha'],'target_sha':attempt['target_sha'],
                   'policy_digest':attempt['policy_digest'],'spec_digest':attempt['spec_digest'],
                   'mandate':card['mandate'],'verdict':'request_changes' if findings else 'approve',
                   'findings':findings,'verification_run':['isolated synthetic attack'], 'prior_findings':[]}
        assert kb.complete_task(conn, card['task_id'], expected_run_id=lane_run.current_run_id,
                                metadata={'bounded_review':receipt})
        if card['mandate'] != 'scope':
            assert not kb.complete_task(conn, owner, force=True)
    final = state.get_attempt(conn, owner)
    assert final['completed_rounds'] == 3
    assert final['state'] == ('held' if scope_changes else 'approved')
    assert cohort.start_cohort(conn, owner, lanes=lanes, expected_version=final['version']) is None
    assert kb.complete_task(conn, owner, force=True) is (not scope_changes)
    conn.close()
