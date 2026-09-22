"""A→B→A under the real gateway runtime scope keeps board state isolated."""
import json
from pathlib import Path

from agent.secret_scope import is_multiplex_active, set_multiplex_active
from gateway.run import _profile_runtime_scope
from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect, write_txn


def test_two_profile_scopes_keep_incidents_counters_and_lessons_separate(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'a'))
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda p:True)
    homes={name:tmp_path/name for name in ('a','b')}
    for name,home in homes.items():
        home.mkdir()
        (home/'config.yaml').write_text(json.dumps({'kanban':{'review_feedback':{'postmortem_profile':f'diagnostic_{name}','validator_profile':f'validator_{name}'}}}))
    prior=is_multiplex_active()
    set_multiplex_active(True)
    ids={}
    try:
        for name in ('a','b','a'):
            with _profile_runtime_scope(homes[name],hydrate_secrets=False):
                conn=connect(homes[name]/'isolated-board.db')
                if name in ids:
                    attempt=state.get_attempt(conn,'t_11111111')
                    assert attempt['board_id']==ids[name]
                    assert attempt['completed_rounds']==1
                    assert conn.execute('SELECT COUNT(*) FROM workflow_lessons').fetchone()[0]==1
                    conn.close()
                    continue
                with monkeypatch.context() as local:
                    local.setattr(kb,'_new_task_id',lambda:'t_11111111')
                    task=kb.create_task(conn,title='same task id',assignee=None)
                board=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0]
                ids[name]=board
                state.enroll_review(conn,task,expected_status='ready',expected_run_id=None,
                    board_id=board,spec_digest='a'*64,base_sha='b'*40,target_sha='c'*40,
                    implementer_maker='openai',roster=sorted(state.REQUIRED_LANES),
                    consumed={'rounds':1 if name=='a' else 2,'recovery':0,'active_seconds':0},
                    compatibility=writer_receipts(conn),decision='synthetic scope')
                with write_txn(conn):
                    kb._append_event(conn,task,'timed_out',{'elapsed_seconds':12,'limit_seconds':10},run_id=1)
                incident=dict(conn.execute('SELECT * FROM workflow_incidents').fetchone())
                event=json.loads(incident['source_events'])[0]
                spawned=[]
                dispatch.dispatch_once(conn,spawn_fn=lambda t,w:spawned.append(t),max_spawn=1)
                reporter=spawned[0]
                assert reporter.assignee==f'diagnostic_{name}'
                assert incident['board_id']==board
                if name=='a':
                    fact = dict(conn.execute('SELECT id,kind,created_at FROM task_events WHERE id=?', (event,)).fetchone())
                    report={'incident_id':incident['id'],'citations':[event],'facts':[fact],
                        'hypotheses':[],'confidence':'high','contributing_conditions':[],'missed_gates':[],
                        'confidence_basis':'cited_event_observation_only',
                        'recovery_recommendation':'operator_decision_required','validation_needed':['deterministic_replay'],'owner':task,
                        'proposed_change':{'kind':'procedural_evidence','approval_required':False,'record':{
                            'procedure_id':'record-worker-deadline','failure_shape':'worker-timeout','source_event':event,
                            'required_evidence':['elapsed_seconds','limit_seconds'],'invocation':'hermes kanban runs <task-id> --json'}}}
                    assert kb.complete_task(conn,reporter.id,summary='Diagnostic receipt submitted',expected_run_id=reporter.current_run_id,metadata={'postmortem':report})
                else:
                    assert conn.execute('SELECT COUNT(*) FROM workflow_lessons').fetchone()[0]==0
                conn.close()
        assert ids['a']!=ids['b']
    finally:
        set_multiplex_active(prior)
