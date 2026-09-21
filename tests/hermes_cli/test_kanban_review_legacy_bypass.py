"""Legacy uncertainty and a recreated card cannot erase prior activity."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from tests.hermes_cli.test_kanban_review_readiness import enrollment


def test_unrun_legacy_block_is_not_fresh_zero_usage(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    with connect(tmp_path/'board.db') as conn:
        task = kb.create_task(conn,title='uncertain imported work',assignee='builder')
        assert kb.block_task(conn,task,kind='needs_input',reason='history not imported')
        values = enrollment(conn)
        values['expected_status'] = kb.get_task(conn,task).status
        with pytest.raises(ValueError,match='history adjudication'):
            state.enroll_review(conn,task,**values,compatibility=writer_receipts(conn))
        assert state.get_attempt(conn,task) is None


def test_clean_legacy_review_is_still_a_consumed_round(tmp_path, monkeypatch):
    from hermes_cli.kanban_review_legacy import inspect_history
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    with connect(tmp_path/'board.db') as conn:
        task = kb.create_task(conn,title='clean legacy review',assignee='builder')
        run = kb.claim_task(conn,task)
        assert kb.request_review(conn,task,expected_run_id=run.current_run_id)
        run = kb.claim_review_task(conn,task)
        assert kb.complete_task(conn,task,expected_run_id=run.current_run_id,summary='clean old roster')
        observed = inspect_history(conn,task)
        assert observed['lower_bounds']['rounds'] == 1


def test_closed_run_with_live_process_is_not_a_safe_boundary(tmp_path, monkeypatch):
    import subprocess
    import sys
    from hermes_cli.kanban_db_dispatch import _set_worker_pid
    from hermes_cli.kanban_review_legacy import adjudicate, inspect_history
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    worker = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    try:
        with connect(tmp_path/'board.db') as conn:
            task = kb.create_task(conn,title='finishing process',assignee='builder')
            run = kb.claim_task(conn,task)
            _set_worker_pid(conn,task,worker.pid)
            assert kb.complete_task(conn,task,expected_run_id=run.current_run_id)
            observed = inspect_history(conn,task)
            receipt = {**enrollment(conn), 'operation':'legacy-history','disposition':'enroll',
                       'expected_status':observed['status'], 'history_digest':observed['history_digest'],
                       'spec_digest':observed['spec_digest'], 'approved_by':'Justin',
                       'compatibility':writer_receipts(conn),'consumed':{'rounds':1,'recovery':0,'active_seconds':30}}
            receipt.pop('expected_assignee')  # Legacy admission binds the inspected history.
            with pytest.raises(ValueError,match='physical quiescence'):
                adjudicate(conn,task,receipt)
            assert state.get_attempt(conn,task) is None
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_preserved_conservative_counts_cannot_be_reduced(tmp_path, monkeypatch):
    from hermes_cli.kanban_review_legacy import adjudicate, inspect_history
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    with connect(tmp_path/'board.db') as conn:
        task = kb.create_task(conn,title='prior accepted counts',assignee='builder')
        observed = inspect_history(conn,task)
        receipt = {**enrollment(conn), 'operation':'legacy-history', 'disposition':'preserve_legacy',
                   'history_digest':observed['history_digest'],'spec_digest':observed['spec_digest'],
                   'approved_by':'Justin','compatibility':None,
                   'consumed':{'rounds':2,'recovery':1,'active_seconds':30}}
        receipt.pop('expected_assignee')
        adjudicate(conn,task,receipt)
        observed = inspect_history(conn,task)
        receipt.update(disposition='enroll',history_digest=observed['history_digest'],
                       compatibility=writer_receipts(conn),consumed={'rounds':0,'recovery':0,'active_seconds':0})
        with pytest.raises(ValueError,match='conservative consumed'):
            adjudicate(conn,task,receipt)
        assert state.get_attempt(conn,task) is None
