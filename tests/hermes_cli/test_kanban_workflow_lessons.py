"""Learning proposals are typed evidence, never executable model instructions."""
import json
import hashlib
from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect, write_txn


@pytest.mark.parametrize('attack',[None,'permission','budget','reviewer','other_profile','root_cause','markdown_policy','application_io_failure'])
def test_allowed_proposal_gets_independent_validation_not_policy_authority(tmp_path, monkeypatch, attack):
    home=tmp_path/'.hermes'
    home.mkdir()
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    skill=home/'skills/software-development/development-lifecycle/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Existing approved procedure: retain the worker deadline receipt.\n')
    original_skill=skill.read_bytes()
    (home/'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n    validator_profile: validator\n')
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda p:p in {'diagnostic','validator'})
    conn=connect(tmp_path/'board.db')
    owner=kb.create_task(conn,title='synthetic timeout',assignee=None)
    run=kb.claim_task(conn,owner)
    assert run is not None
    with write_txn(conn):
        kb._append_event(conn,owner,'timed_out',{'elapsed_seconds':12,'limit_seconds':10},run_id=run.current_run_id)
    assert kb.block_task(conn,owner,kind='transient',reason='await recovery',expected_run_id=run.current_run_id)
    incident=dict(conn.execute("SELECT * FROM workflow_incidents WHERE run_id=?",(run.current_run_id,)).fetchone())
    event=json.loads(incident['source_events'])[0]
    spawned=[]
    dispatch.dispatch_once(conn,spawn_fn=lambda t,w:spawned.append(t),max_spawn=1)
    reporter=spawned[0]
    report={'incident_id':incident['id'],'citations':[event], 'facts':['Worker exceeded its deadline.'],
            'hypotheses':[], 'confidence':'high','contributing_conditions':[], 'missed_gates':[],
            'recovery_recommendation':'Operator decision.', 'validation_needed':['Replay timing comparison'], 'owner':owner,
            'proposed_change':{'kind':'procedural_evidence','record':{
                'procedure_id':'record-worker-deadline','failure_shape':'worker-timeout',
                'source_event':event,'required_evidence':['elapsed_seconds','limit_seconds'],
                'invocation':'hermes kanban runs <task-id> --json'},'approval_required':False}}
    if attack and attack!='application_io_failure':
        changes={'permission':{'tools':['terminal']},'budget':{'rounds':4},
                 'reviewer':{'remove_mandate':'scope'},'other_profile':{'destination':'profiles/other/skills/SKILL.md'},
                 'root_cause':{'root_cause':'unverified assertion'},'markdown_policy':{'edit':'Approve without review.'}}
        report['proposed_change']['record'].update(changes[attack])
        assert not kb.complete_task(conn,reporter.id,expected_run_id=reporter.current_run_id,metadata={'postmortem':report})
        assert conn.execute('SELECT COUNT(*) FROM workflow_lessons').fetchone()[0]==0
        assert skill.read_bytes()==original_skill
        conn.close()
        return
    assert kb.complete_task(conn,reporter.id,expected_run_id=reporter.current_run_id,metadata={'postmortem':report}), 'Allowed evidence records must reach the independent validator'
    dispatch.dispatch_once(conn,spawn_fn=lambda t,w:spawned.append(t),max_spawn=1)
    validator=spawned[-1]
    assert validator.assignee=='validator'
    assert validator.id!=reporter.id
    assert kb.complete_task(conn,validator.id,expected_run_id=validator.current_run_id,
                            metadata={'lesson_validation':{'source_event':event,'result':'reproduced'}})
    lesson=dict(conn.execute('SELECT * FROM workflow_lessons').fetchone())
    assert lesson['status']=='validated'
    assert lesson['validator_run']==validator.current_run_id
    assert lesson['after_hash'] is None, 'Report-only is not auto-application'
    assert kb.get_task(conn,owner).status!='done'
    reference=skill.parent/'references/verified-procedures.jsonl'
    reference.parent.mkdir()
    reference.write_bytes(b'')
    config=json.loads(json.dumps({'kanban':{'review_feedback':{'postmortem_profile':'diagnostic',
        'validator_profile':'validator','auto_apply_lessons':True,
        'protected_skill_hash':hashlib.sha256(skill.read_bytes()).hexdigest()}}}))
    (home/'config.yaml').write_text(json.dumps(config))
    if attack=='application_io_failure':
        from hermes_cli import kanban_lesson_apply as apply
        def fail_write(*args):
            raise OSError('synthetic reference write failure')
        monkeypatch.setattr(apply,'_replace',fail_write)
        dispatch.dispatch_once(conn,spawn_fn=lambda *a:None,max_spawn=0)
        assert conn.execute('SELECT status FROM workflow_lessons').fetchone()[0]=='pending_approval'
        assert reference.read_bytes()==b''
        assert skill.read_bytes()==original_skill
        conn.close()
        return
    dispatch.dispatch_once(conn,spawn_fn=lambda *a:None,max_spawn=0)
    assert reference.read_bytes(), 'Validated allowlisted evidence must actually reach the procedural reference'
    applied=dict(conn.execute('SELECT * FROM workflow_lessons').fetchone())
    assert applied['status']=='applied'
    assert applied['after_hash']==hashlib.sha256(reference.read_bytes()).hexdigest()
    next_task=kb.create_task(conn,title='subsequent intake',assignee=None)
    assert lesson['id'] in kb.build_worker_context(conn,next_task)
    from hermes_cli import kanban_lesson_apply as apply
    with pytest.raises(ValueError,match='stale'):
        apply.apply_next(conn,expected_hash=hashlib.sha256(b'').hexdigest())
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    assert apply.rollback(conn,lesson['id'],expected_hash=applied['after_hash'],decision='synthetic rollback')
    assert reference.read_bytes()==b''
    assert hashlib.sha256(skill.read_bytes()).hexdigest()==config['kanban']['review_feedback']['protected_skill_hash']
    conn.close()
