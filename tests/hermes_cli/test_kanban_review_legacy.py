"""Legacy adoption is an explicit, snapshot-scoped operator decision."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.test_kanban_review_operator import invoke
from tests.hermes_cli.review_readiness_helpers import writer_receipts


@pytest.mark.parametrize('disposition', ['preserve_legacy', 'enroll'])
def test_public_legacy_history_adjudication_retains_receipts(tmp_path, monkeypatch, capsys, disposition):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='old ask', body='approved acceptance', assignee='builder')
        run = kb.claim_task(conn, task)
        assert kb.request_review(conn, task, expected_run_id=run.current_run_id)
        run = kb.claim_review_task(conn, task)
        assert kb.request_changes(conn, task, reason='old required fix', expected_run_id=run.current_run_id)[0]
        assert invoke(tmp_path, task, {'operation':'legacy-history','disposition':'inspect'}) == 0
        observed = json.loads(capsys.readouterr().out)
        assert observed['lower_bounds']['rounds'] >= 1
        receipt = {'operation':'legacy-history','disposition':disposition,
                   'history_digest':observed['history_digest'],'board_id':observed['board_id'],
                   'expected_status':observed['status'],'expected_run_id':None,
                   'spec_digest':observed['spec_digest'], 'base_sha':'b'*40,'target_sha':'c'*40,
                   'approved_by':'Justin','decision':'accept conservative history; not retroactive approval',
                   'consumed':{'rounds':2,'recovery':1,'active_seconds':30},
                   'compatibility':writer_receipts(conn),'implementer_maker':'openai',
                   'roster':sorted(state.REQUIRED_LANES)}
        assert invoke(tmp_path, task, receipt) == 0
        after = state.get_attempt(conn, task)
        if disposition == 'enroll':
            assert (after['completed_rounds'],after['recovery_used'],after['active_seconds']) == (2,1,30)
            assert after['state'] == 'preflight'
            assert kb.claim_task(conn, task) is None
        else:
            assert after is None
            assert kb.claim_task(conn, task) is not None
        journal = conn.execute('SELECT receipt,history FROM review_legacy_adjudications WHERE task_id=?', (task,)).fetchone()
        assert json.loads(journal['receipt'])['decision'] == receipt['decision']
        assert json.loads(journal['history'])['runs'], 'Prior run evidence is retained, not just counts'
        assert invoke(tmp_path, task, receipt) != 0, 'Consumed history CAS cannot grant another allowance'


@pytest.mark.parametrize('unknown', [True, False])
def test_unknown_legacy_history_only_gets_explicit_linked_successor(tmp_path, monkeypatch, capsys, unknown):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='uncertain old attempt', assignee='builder')
        assert kb.block_task(conn, task, reason='incomplete imported history', kind='needs_input')
        successor = kb.create_task(conn, title='explicit bounded continuation', assignee='builder')
        assert invoke(tmp_path, task, {'operation':'legacy-history','disposition':'inspect'}) == 0
        observed = json.loads(capsys.readouterr().out)
        receipt = {'operation':'legacy-history','disposition':'successor',
                   'history_digest':observed['history_digest'],'board_id':observed['board_id'],
                   'expected_status':observed['status'],'expected_run_id':None,
                   'spec_digest':observed['spec_digest'],'base_sha':'b'*40,'target_sha':'c'*40,
                   'approved_by':'Justin','decision':'preserve unknown predecessor; authorize one finite new round',
                   'consumed':None if unknown else {'rounds':1,'recovery':0,'active_seconds':30},
                   'compatibility':writer_receipts(conn),'implementer_maker':'openai',
                   'roster':sorted(state.REQUIRED_LANES),
                   'successor':{'task_id':successor,'base_sha':'b'*40,'target_sha':'c'*40,
                                'allowance':{'rounds':1,'recovery':0,'active_seconds':60},
                                'remaining_finding_events':[]}}
        assert invoke(tmp_path, task, receipt) == 0
        old, new = state.get_attempt(conn,task), state.get_attempt(conn,successor)
        assert old['state'] == 'held'
        assert json.loads(capsys.readouterr().out)['attempt']['state'] == old['state']
        assert old['completed_rounds'] == (old['policy']['rounds'] if unknown else 1)
        assert new['policy']['rounds'] == 1
        assert conn.execute('SELECT predecessor_id FROM review_successors WHERE successor_id=?', (new['id'],)).fetchone()[0] == old['id']
        assert kb.claim_task(conn,task) is None
        assert invoke(tmp_path, task, receipt) != 0
