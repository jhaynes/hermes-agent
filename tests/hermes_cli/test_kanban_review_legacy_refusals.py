"""Refused adoption leaves the legacy card and journal unchanged."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.test_kanban_review_operator import invoke
from tests.hermes_cli.review_readiness_helpers import writer_receipts


@pytest.mark.parametrize('case', ['running','frozen','child','unknown','low','stale','worker','ask','exhausted'])
def test_legacy_admission_refusals_and_exhaustion_are_atomic(tmp_path, monkeypatch, capsys, case):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='old ask', assignee='builder')
        run = kb.claim_task(conn,task)
        if case != 'running':
            assert kb.request_review(conn,task,expected_run_id=run.current_run_id)
            if case != 'frozen':
                run = kb.claim_review_task(conn,task)
                assert kb.request_changes(conn,task,reason='old issue',expected_run_id=run.current_run_id)[0]
        if case == 'child':
            assert kb.complete_task(conn,task,summary='legacy owner finished')
            child = kb.create_task(conn,title='old frozen review lane',assignee='reviewer',parents=[task])
            assert kb.claim_task(conn,child)
        assert invoke(tmp_path,task,{'operation':'legacy-history','disposition':'inspect'}) == 0
        observed = json.loads(capsys.readouterr().out)
        receipt = {'operation':'legacy-history','disposition':'enroll',
                   'history_digest':observed['history_digest'],'board_id':observed['board_id'],
                   'expected_status':observed['status'],'expected_run_id':observed['expected_run_id'],
                   'spec_digest':observed['spec_digest'],'base_sha':'b'*40,'target_sha':'c'*40,
                   'approved_by':'Justin','decision':'conservative adoption','consumed':{'rounds':1,'recovery':1,'active_seconds':30},
                   'compatibility':writer_receipts(conn),'implementer_maker':'openai','roster':sorted(state.REQUIRED_LANES)}
        if case == 'unknown':
            receipt['consumed'] = None
        elif case == 'low':
            receipt['consumed']['rounds'] = 0
        elif case == 'stale':
            kb.add_comment(conn, task, author='operator', body='new receipt')
        elif case == 'worker':
            monkeypatch.setenv('HERMES_KANBAN_TASK',task)
        elif case == 'ask':
            receipt['spec_digest'] = '0'*64
        elif case == 'exhausted':
            receipt['consumed']['rounds'] = 3
        status = kb.get_task(conn,task).status
        result = invoke(tmp_path,task,receipt)
        if case == 'exhausted':
            assert result == 0
            assert state.get_attempt(conn,task)['state'] == 'held'
            assert kb.claim_task(conn,task) is None
            assert not kb.complete_task(conn,task,summary='Attempted completion',force=True)
        else:
            assert result != 0
            assert state.get_attempt(conn,task) is None
            assert kb.get_task(conn,task).status == status
            assert not conn.execute('SELECT 1 FROM review_legacy_adjudications').fetchone()
