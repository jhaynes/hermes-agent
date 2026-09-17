"""Public CLI enrollment transports the full pinned contract to admission."""
import argparse
import json

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def test_cli_enrollment_reservation_and_worker_guard_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    path=tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB',str(path))
    conn=connect(path)
    task=kb.create_task(conn,title='CLI enrollment',assignee='builder')
    receipt={'expected_status':'ready','expected_run_id':None,
        'board_id':conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        'spec_digest':'a'*64,'base_sha':'b'*40,'target_sha':'c'*40,'implementer_maker':'openai',
        'roster':['tests','quality','architecture','style','breaker_a','breaker_b','breaker_c','scope'],
        'consumed':{'rounds':0,'recovery':0,'active_seconds':0},
        'compatibility':{'cli':1,'gateway':1,'dashboard':1},'decision':'isolated synthetic operator decision'}
    file=tmp_path/'receipt.json'
    file.write_text(json.dumps(receipt))
    wrapper=argparse.ArgumentParser()
    cli.build_parser(wrapper.add_subparsers(dest='command'))
    args=wrapper.parse_args(['kanban','enroll-review',task,'--receipt',str(file)])
    assert cli.kanban_command(args)==0
    enrolled=json.loads(capsys.readouterr().out)
    assert enrolled['spec_digest']==receipt['spec_digest']
    assert enrolled['roster']==receipt['roster']
    assert kb.claim_task(conn,task) is None
    args=wrapper.parse_args(['kanban','reserve-review-action',task,'--category','preflight','--expected-version','0'])
    assert cli.kanban_command(args)==0
    assert json.loads(capsys.readouterr().out)['action_id']
    run=kb.claim_task(conn,task)
    assert run is not None
    assert kb.request_review(conn,task,expected_run_id=run.current_run_id)
    lanes={name:{'profile':'reviewer','provider':'anthropic','model':'claude-sonnet-4-5',
                 'workspace':str(tmp_path/name)} for name in receipt['roster']}
    lane_file=tmp_path/'lanes.json'
    lane_file.write_text(json.dumps(lanes))
    args=wrapper.parse_args(['kanban','start-review-cohort',task,'--lanes',str(lane_file),'--expected-version','1'])
    assert cli.kanban_command(args)==0
    assert json.loads(capsys.readouterr().out)['round_id']
    conn.close()
