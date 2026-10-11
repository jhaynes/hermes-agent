"""A hidden schema tool cannot be invoked to escape review reservations."""
import json

from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_review_state as state
from hermes_cli import kanban_review_cohort as cohort
from hermes_cli.kanban_db_connect import connect


def test_reviewer_cannot_spawn_delegate_or_replacement_card(tmp_path, monkeypatch):
    from tools import delegate_tool
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    conn = connect(path)
    try:
        owner = kb.create_task(conn, title='approved ask', assignee='builder')
        attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None, expected_assignee='builder',
            board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0], spec_digest='a'*64,
            base_sha='b'*40,target_sha='c'*40,implementer_maker='openai',roster=sorted(state.REQUIRED_LANES),
            consumed={'rounds':0,'recovery':0,'active_seconds':0},compatibility=writer_receipts(conn),decision='synthetic')
        state.reserve_action(conn, owner, category='preflight',expected_version=0)
        run = kb.claim_task(conn, owner)
        cohort.record_runtime_route(conn, owner, run.current_run_id,provider='openai',model='gpt-5',isolated=False)
        assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
        lanes = {name:{'profile':'reviewer','provider':'anthropic','model':'claude-sonnet-4-5',
                       'workspace':str(tmp_path/name)} for name in attempt['roster']}
        cohort.start_cohort(conn,owner,lanes=lanes,expected_version=1)
        reviewer = conn.execute('SELECT task_id FROM review_members LIMIT 1').fetchone()[0]
        worker = kb.claim_task(conn, reviewer)
        monkeypatch.setenv('HERMES_KANBAN_TASK',reviewer)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(worker.current_run_id))
        def forbidden(*args, **kwargs):
            raise AssertionError('Nested review reached credential/launch preparation')
        monkeypatch.setattr(delegate_tool, '_resolve_delegation_credentials', forbidden)
        result = json.loads(delegate_tool.delegate_task(goal='nested review', parent_agent=object()))
        assert 'nested' in result['error'].lower()
        before = len(kb.list_tasks(conn))
        import pytest
        with pytest.raises(ValueError, match='nested'):
            kb.create_task(conn,title='replacement review',assignee='reviewer',creator_task_id=reviewer)
        assert len(kb.list_tasks(conn)) == before
    finally:
        conn.close()
