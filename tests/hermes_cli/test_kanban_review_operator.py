"""Operator decisions use the supported receipt CLI, not a worker's prose."""
import argparse
import json
import hashlib
import pytest
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from tests.hermes_cli.review_evidence_helpers import scope_evidence
from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect, write_txn


def invoke(tmp_path, task, receipt):
    path = tmp_path / 'decision.json'
    path.write_text(json.dumps(receipt))
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest='command'))
    return cli.kanban_command(parser.parse_args(['kanban','enroll-review',task,'--receipt',str(path)]))


@pytest.mark.parametrize('operation', ['resume', 'cancel', 'amend'])
def test_scoped_operator_decision_preserves_budgets(tmp_path, monkeypatch, operation):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='approved ask', assignee='builder')
        attempt = state.enroll_review(conn, task, expected_status='ready', expected_run_id=None,
            board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
            spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
            roster=sorted(state.REQUIRED_LANES), consumed={'rounds':1,'recovery':1,'active_seconds':25},
            compatibility=writer_receipts(conn), decision='synthetic')
        with write_txn(conn):
            state.hold(conn, attempt, 'operator_hold')
        decision = {'operation':operation, 'attempt_id':attempt['id'], 'expected_version':1,
                    'board_id':attempt['board_id'], 'spec_digest':attempt['spec_digest'],
                    'base_sha':attempt['base_sha'], 'target_sha':attempt['target_sha'],
                    'approved_by':'Justin', 'decision':'synthetic scoped decision', 'findings':[]}
        if operation == 'amend':
            ask = 'approved ask with explicit clarification'
            decision['amendment'] = {'ask':ask, 'spec_digest':hashlib.sha256(ask.encode()).hexdigest()}
        # Workers cannot turn the same receipt into self-authorization.
        monkeypatch.setenv('HERMES_KANBAN_TASK', task)
        assert invoke(tmp_path, task, decision) != 0
        assert state.get_attempt(conn, task)['state'] == 'held'
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        assert invoke(tmp_path, task, decision) == 0
        after = state.get_attempt(conn, task)
        assert (after['completed_rounds'],after['recovery_used'],after['active_seconds']) == (1,1,25)
        assert after['policy_digest'] == attempt['policy_digest']
        assert after['state'] == {'resume':'preflight','cancel':'cancelled','amend':'held'}[operation]
        assert invoke(tmp_path, task, decision) != 0, 'Consumed CAS cannot be replayed as new authority'
        assert kb.claim_task(conn, task) is None, 'A decision does not itself reserve a launch'
        if operation == 'amend':
            clone = kb.create_task(conn, title='renamed original ask', assignee='builder')
            with pytest.raises(ValueError, match='lineage'):
                state.enroll_review(conn, clone, expected_status='ready', expected_run_id=None,
                    board_id=attempt['board_id'], spec_digest=attempt['spec_digest'],
                    base_sha=attempt['base_sha'], target_sha=attempt['target_sha'], implementer_maker='openai',
                    roster=attempt['roster'], consumed={'rounds':0,'recovery':0,'active_seconds':0},
                    compatibility=attempt['compatibility']['selection'], decision='recreation is not authority')


@pytest.mark.parametrize('diagnostic', [False, True])
def test_exhausted_predecessor_needs_explicit_finite_successor(tmp_path, monkeypatch, diagnostic):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='exhausted ask', assignee='builder')
        attempt = state.enroll_review(conn, task, expected_status='ready', expected_run_id=None,
            board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
            spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
            roster=sorted(state.REQUIRED_LANES), consumed={'rounds':3,'recovery':2,'active_seconds':7200},
            compatibility=writer_receipts(conn), decision='known exhausted history')
        successor = kb.create_task(conn, title='explicit next phase', assignee='builder')
        if diagnostic:
            from hermes_cli.config import save_config
            from hermes_cli.kanban_postmortem import queue_reports
            save_config({'kanban':{'review_feedback':{'postmortem_profile':'diagnostic'}}})
            failed = kb.create_task(conn,title='unrelated incident',assignee=None)
            assert kb.block_task(conn,failed,kind='capability',reason='synthetic')
            queue_reports(conn)
            successor = conn.execute('SELECT task_id FROM workflow_postmortems LIMIT 1').fetchone()[0]
        decision = {'operation':'successor', 'attempt_id':attempt['id'], 'expected_version':0,
                    'board_id':attempt['board_id'], 'spec_digest':attempt['spec_digest'],
                    'base_sha':attempt['base_sha'], 'target_sha':attempt['target_sha'],
                    'approved_by':'Justin', 'decision':'synthetic one-round successor', 'findings':[],
                    'successor':{'task_id':successor,'base_sha':'b'*40,'target_sha':'d'*40,
                                 'compatibility':writer_receipts(conn),
                                 'allowance':{'rounds':1,'recovery':0,'active_seconds':60}}}
        if diagnostic:
            assert invoke(tmp_path, task, decision) != 0
            assert state.get_attempt(conn,successor) is None
            return
        assert invoke(tmp_path, task, decision) == 0
        after = state.get_attempt(conn, task)
        new = state.get_attempt(conn, successor)
        assert new['id'] != attempt['id']
        assert new['policy']['rounds'] == 1 and new['policy']['recovery'] == 0
        assert new['spec_digest'] == attempt['spec_digest']
        assert after['state'] == 'held'
        assert (after['completed_rounds'], after['recovery_used'], after['active_seconds']) == (3,2,7200)
        assert kb.claim_task(conn, task) is None
        assert kb.claim_task(conn, successor) is None
        assert invoke(tmp_path, task, decision) != 0
        state.reserve_action(conn, successor, category='preflight', expected_version=0)
        assert kb.claim_task(conn, successor) is not None


@pytest.mark.parametrize('authorized', [True, False])
def test_parent_finding_disposition_retains_original_receipt(tmp_path, monkeypatch, authorized):
    from hermes_cli import kanban_review_cohort as cohort
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='approved ask', assignee='builder')
        attempt = state.enroll_review(conn, task, expected_status='ready', expected_run_id=None,
            board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
            spec_digest='a'*64,base_sha='b'*40,target_sha='c'*40,implementer_maker='openai',
            roster=sorted(state.REQUIRED_LANES),consumed={'rounds':0,'recovery':0,'active_seconds':0},
            compatibility=writer_receipts(conn),decision='synthetic')
        state.reserve_action(conn,task,category='preflight',expected_version=0)
        run = kb.claim_task(conn,task)
        cohort.record_runtime_route(conn,task,run.current_run_id,provider='openai',model='gpt-5',isolated=False)
        assert kb.request_review(conn,task,expected_run_id=run.current_run_id)
        lanes = {name:{'profile':'reviewer','provider':'anthropic','model':'claude-sonnet-4-5',
                       'workspace':str(tmp_path/name)} for name in attempt['roster']}
        round_id = cohort.start_cohort(conn,task,lanes=lanes,expected_version=1)
        evidence = {'kind':'reasoned','reasoning':'synthetic unsupported finding'}
        for member in conn.execute('SELECT * FROM review_members WHERE round_id=?',(round_id,)).fetchall():
            child = member['task_id']
            run = kb.claim_task(conn,child)
            cohort.record_runtime_route(conn,child,run.current_run_id,provider='anthropic',model='claude-sonnet-4-5',isolated=True)
            finding = {'severity':'low','location':{'path':'subject.py','line':1},'evidence':evidence,'required_change':'synthetic change'}
            result = {key:attempt[key] for key in ('board_id','base_sha','target_sha','policy_digest','spec_digest')}
            result.update(attempt_id=attempt['id'],round_id=round_id,mandate=member['mandate'],task_id=child,run_id=run.current_run_id,
                          verdict='request_changes' if member['mandate']=='scope' else 'approve',
                          findings=[finding] if member['mandate']=='scope' else [],verification_run=[evidence],prior_findings=[],scope=scope_evidence(evidence))
            assert kb.complete_task(conn,child,expected_run_id=run.current_run_id,metadata={'bounded_review':result})
        current = state.get_attempt(conn,task)
        original = conn.execute("SELECT receipt FROM review_members WHERE mandate='scope'").fetchone()[0]
        finding_id = cohort.prior_findings(conn,attempt['id'],'scope')[0]['finding_id']
        decision = {key:current[key] for key in ('board_id','spec_digest','base_sha','target_sha')}
        decision.update(operation='reject_finding',attempt_id=current['id'],expected_version=current['version'],approved_by='Justin',
                        decision='parent rejects unsupported interpretation',findings=[finding_id],
                        disposition={'finding_id':finding_id,'evidence':{'kind':'reasoned','reasoning':'original ask explicitly requires this behavior'}})
        if authorized:
            assert invoke(tmp_path,task,decision) == 0
        assert conn.execute("SELECT receipt FROM review_members WHERE mandate='scope'").fetchone()[0] == original
        annotated = cohort.prior_findings(conn,attempt['id'],'scope')[0]
        if authorized:
            assert annotated['disposition']['status'] == 'rejected'
        assert annotated['provenance']['task_id']
        assert state.get_attempt(conn,task)['state'] == 'repair', 'Adjudication is not a new cohort approval'
        current = state.get_attempt(conn,task)
        state.reserve_action(conn,task,category='repair',expected_version=current['version'])
        repair = kb.claim_task(conn,task)
        assert kb.request_review(conn,task,expected_run_id=repair.current_run_id,metadata={'bounded_review':{'target_sha':'d'*40}})
        current = state.get_attempt(conn,task)
        state.reserve_action(conn,task,category='preflight',expected_version=current['version'])
        preflight = kb.claim_review_task(conn,task)
        cohort.record_runtime_route(conn,task,preflight.current_run_id,provider='openai',model='gpt-5',isolated=False)
        assert kb.request_review(conn,task,expected_run_id=preflight.current_run_id)
        current = state.get_attempt(conn,task)
        next_round = cohort.start_cohort(conn,task,lanes=lanes,expected_version=current['version'])
        child = conn.execute("SELECT task_id FROM review_members WHERE round_id=? AND mandate='scope'",(next_round,)).fetchone()[0]
        worker = kb.claim_task(conn,child)
        cohort.record_runtime_route(conn,child,worker.current_run_id,provider='anthropic',model='claude-sonnet-4-5',isolated=True)
        result.update(round_id=next_round,target_sha='d'*40,mandate='scope',task_id=child,run_id=worker.current_run_id,
                      verdict='approve',findings=[],prior_findings=[{'finding_id':finding_id,'status':'rejected','evidence':evidence}])
        assert kb.complete_task(conn,child,expected_run_id=worker.current_run_id,metadata={'bounded_review':result})
        assert conn.execute('SELECT state FROM review_members WHERE task_id=?',(child,)).fetchone()[0] == ('received_valid' if authorized else 'invalid')
