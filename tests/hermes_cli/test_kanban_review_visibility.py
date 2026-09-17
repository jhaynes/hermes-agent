"""Existing show/context surfaces expose managed review state."""
import argparse
import json
from tests.hermes_cli.review_readiness_helpers import writer_receipts

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect


def test_show_json_and_new_worker_brief_include_pinned_attempt(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    path=tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB',str(path))
    conn=connect(path)
    task=kb.create_task(conn,title='visibility',assignee='builder')
    attempt=state.enroll_review(conn,task,expected_status='ready',expected_run_id=None,
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],spec_digest='a'*64,
        base_sha='b'*40,target_sha='c'*40,implementer_maker='openai',roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds':2,'recovery':1,'active_seconds':20},compatibility=writer_receipts(conn),decision='synthetic')
    wrapper=argparse.ArgumentParser()
    cli.build_parser(wrapper.add_subparsers(dest='command'))
    args=wrapper.parse_args(['kanban','show',task,'--json'])
    assert cli.kanban_command(args)==0
    data=json.loads(capsys.readouterr().out)
    assert data.get('workflow',{}).get('attempt',{}).get('id')==attempt['id'], 'Existing show JSON must expose enrollment'
    assert data['workflow']['attempt']['completed_rounds']==2
    assert data['workflow']['attempt']['recovery_used']==1
    brief=kb.build_worker_context(conn,task)
    assert attempt['id'] in brief
    assert attempt['policy_digest'] in brief
    assert attempt['spec_digest'] in brief
    state.reserve_action(conn, task, category='preflight', expected_version=attempt['version'])
    run = kb.claim_task(conn, task)
    args = wrapper.parse_args(['kanban', 'runs', task, '--json'])
    assert cli.kanban_command(args) == 0
    runs = json.loads(capsys.readouterr().out)
    assert runs[0].get('workflow', {}).get('attempt_id') == attempt['id']
    assert runs[0]['workflow']['action']['run_id'] == run.current_run_id
    assert runs[0]['workflow']['spec_digest'] == attempt['spec_digest']
    conn.close()
