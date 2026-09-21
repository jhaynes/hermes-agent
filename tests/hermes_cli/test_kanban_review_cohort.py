"""Whole-cohort completion uses actual lane-card lifecycle, never verdict counts."""
import importlib.util
import pytest
from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from tests.hermes_cli.review_evidence_helpers import scope_evidence
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect


@pytest.mark.parametrize('consumed,scope_changes', [(2,False), (2,True), (0,True)])
def test_cohort_requires_scope_and_only_allows_bounded_repair(tmp_path, monkeypatch, consumed, scope_changes):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    owner = kb.create_task(conn, title='approved ask', assignee='builder', workspace_kind='dir', workspace_path=str(tmp_path))
    attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None,
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
        consumed={'rounds':consumed,'recovery':0,'active_seconds':0},
        compatibility=writer_receipts(conn), decision='conservative adoption')
    state.reserve_action(conn, owner, category='preflight', expected_version=0)
    run = kb.claim_task(conn, owner)
    assert run is not None
    from hermes_cli import kanban_review_cohort as cohort
    cohort.record_runtime_route(conn, owner, run.current_run_id, provider='openai', model='gpt-5', isolated=False)
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
        findings = [{'severity':'high', 'location':{'path':'subject.py','line':1},
                     'evidence':{'kind':'reasoned','reasoning':'synthetic unit-test finding'},
                     'required_change':'remove unrelated behavior'}] if scope_changes and card['mandate']=='scope' else []
        receipt = {'attempt_id':attempt['id'],'board_id':attempt['board_id'],'round_id':round_id,
                   'task_id':card['task_id'],'run_id':lane_run.current_run_id,
                   'base_sha':attempt['base_sha'],'target_sha':attempt['target_sha'],
                   'policy_digest':attempt['policy_digest'],'spec_digest':attempt['spec_digest'],
                   'mandate':card['mandate'],'verdict':'request_changes' if findings else 'approve',
                   'findings':findings,'verification_run':[{'kind':'reasoned','reasoning':'synthetic unit-test review'}], 'prior_findings':[], 'scope':scope_evidence()}
        assert kb.complete_task(conn, card['task_id'], expected_run_id=lane_run.current_run_id,
                                metadata={'bounded_review':receipt})
        if card['mandate'] != 'scope':
            assert not kb.complete_task(conn, owner, force=True)
    final = state.get_attempt(conn, owner)
    if consumed == 0:
        assert final['completed_rounds'] == 1
        assert final['state'] == 'repair'
        with pytest.raises(ValueError,match='owner'):
            state.reserve_action(conn,card['task_id'],category='repair',expected_version=final['version'])
        with pytest.raises(ValueError,match='owner'):
            cohort.start_cohort(conn,card['task_id'],lanes=lanes,expected_version=final['version'])
        state.reserve_action(conn, owner, category='repair', expected_version=final['version'])
        repair = kb.claim_task(conn, owner)
        assert repair is not None, 'Valid rejection must hand one repair back to the original builder'
        assert repair.assignee == 'builder'
        assert kb.request_review(conn, owner, expected_run_id=repair.current_run_id,
                                 metadata={'bounded_review':{'target_sha':'d'*40}})
        updated = state.get_attempt(conn, owner)
        assert updated['state'] == 'preflight'
        assert updated['target_sha'] == 'd'*40
        assert updated['completed_rounds'] == 1
        assert not kb.complete_task(conn, owner, force=True)
        state.reserve_action(conn,owner,category='preflight',expected_version=updated['version'])
        preflight=kb.claim_review_task(conn,owner)
        assert preflight is not None
        cohort.record_runtime_route(conn, owner, preflight.current_run_id, provider='openai', model='gpt-5', isolated=False)
        assert kb.request_review(conn,owner,expected_run_id=preflight.current_run_id)
        updated=state.get_attempt(conn,owner)
        next_round=cohort.start_cohort(conn,owner,lanes=lanes,expected_version=updated['version'])
        scope=conn.execute("SELECT task_id FROM review_members WHERE round_id=? AND mandate='scope'",(next_round,)).fetchone()[0]
        scope_run=kb.claim_task(conn,scope)
        cohort.record_runtime_route(conn,scope,scope_run.current_run_id,provider='anthropic',model='claude-sonnet-4-5',isolated=True)
        incomplete={**receipt,'round_id':next_round,'task_id':scope,'run_id':scope_run.current_run_id,
                    'target_sha':'d'*40,'verdict':'approve','findings':[],'prior_findings':[]}
        assert kb.complete_task(conn,scope,expected_run_id=scope_run.current_run_id,metadata={'bounded_review':incomplete})
        assert conn.execute('SELECT state FROM review_members WHERE task_id=?',(scope,)).fetchone()[0]=='invalid', 'Prior accepted findings require explicit re-attack closure evidence'
        conn.close()
        return
    assert final['completed_rounds'] == 3
    assert final['state'] == ('held' if scope_changes else 'approved')
    if scope_changes:
        from hermes_cli.kanban_diagnostic_evidence import receipts
        incident = conn.execute("SELECT * FROM workflow_incidents WHERE classification='failure' ORDER BY rowid DESC LIMIT 1").fetchone()
        evidence = receipts(conn, incident)
        reviews = [r for r in evidence.values() if r.get('kind') == 'review_receipt']
        assert {r['mandate'] for r in reviews} == set(attempt['roster'])
        scope = next(r for r in reviews if r['mandate'] == 'scope')
        assert scope['verdict'] == 'request_changes' and scope['evidence'][0]['kind'] == 'reasoned'
    assert cohort.start_cohort(conn, owner, lanes=lanes, expected_version=final['version']) is None
    # These synthetic SHAs exercise cohort aggregation, not a real Git target.
    # Actual positive owner completion is covered by test_kanban_review_snapshot.
    assert not kb.complete_task(conn, owner, force=True)
    conn.close()


def test_failed_cohort_replacements_share_recovery_and_cancel_unlaunched_lanes(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda p:True)
    conn=connect(tmp_path/'board.db')
    owner=kb.create_task(conn,title='failed launches',assignee='builder',workspace_kind='dir',workspace_path=str(tmp_path))
    attempt=state.enroll_review(conn,owner,expected_status='ready',expected_run_id=None,
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],spec_digest='a'*64,
        base_sha='b'*40,target_sha='c'*40,implementer_maker='openai',roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds':0,'recovery':0,'active_seconds':0},compatibility=writer_receipts(conn),decision='synthetic')
    state.reserve_action(conn,owner,category='preflight',expected_version=0)
    run=kb.claim_task(conn,owner)
    from hermes_cli import kanban_review_cohort as cohort
    cohort.record_runtime_route(conn, owner, run.current_run_id, provider='openai', model='gpt-5', isolated=False)
    assert kb.request_review(conn,owner,expected_run_id=run.current_run_id)
    from hermes_cli import kanban_review_cohort as cohort
    lanes={name:{'profile':'reviewer','model':'claude-sonnet-4-5','provider':'anthropic','workspace':str(tmp_path/name)} for name in attempt['roster']}
    launches=[]
    def fail(task, workspace):
        launches.append(task.id)
        raise OSError('synthetic launch failure')
    for retry in range(3):
        current=state.get_attempt(conn,owner)
        round_id=cohort.start_cohort(conn,owner,lanes=lanes,expected_version=current['version'],recovery=retry>0)
        dispatch.dispatch_once(conn,spawn_fn=fail,max_spawn=1)
        assert conn.execute('SELECT state FROM review_rounds WHERE id=?',(round_id,)).fetchone()[0]=='invalid'
        assert conn.execute("SELECT COUNT(*) FROM review_actions WHERE attempt_id=? AND state='reserved'",(attempt['id'],)).fetchone()[0]==0
        assert len(launches)==retry+1
    current=state.get_attempt(conn,owner)
    assert current['completed_rounds']==0
    assert current['recovery_used']==2
    assert cohort.start_cohort(conn,owner,lanes=lanes,expected_version=current['version'],recovery=True) is None
    assert state.get_attempt(conn,owner)['state']=='held'
    dispatch.dispatch_once(conn,spawn_fn=fail,max_spawn=1)
    assert len(launches)==3
    conn.close()
