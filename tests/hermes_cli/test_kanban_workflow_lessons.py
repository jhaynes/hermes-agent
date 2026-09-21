"""Learning proposals are typed evidence, never executable model instructions."""
import json
import hashlib
from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect, write_txn


@pytest.mark.parametrize('attack',[None,'permission','budget','reviewer','other_profile','root_cause','markdown_policy','application_io_failure','crash_after_replace','concurrent','validator_failure','path_swap','hash_race','recurrence'])
def test_allowed_proposal_gets_independent_validation_not_policy_authority(tmp_path, monkeypatch, attack):
    home=tmp_path/'.hermes'
    home.mkdir()
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
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
    fact = dict(conn.execute('SELECT id,kind,created_at FROM task_events WHERE id=?', (event,)).fetchone())
    report={'incident_id':incident['id'],'citations':[event], 'facts':[fact],
            'hypotheses':[], 'confidence':'high','contributing_conditions':[], 'missed_gates':[],
            'confidence_basis':'cited_event_observation_only',
            'recovery_recommendation':'operator_decision_required', 'validation_needed':['deterministic_replay'], 'owner':owner,
            'proposed_change':{'kind':'procedural_evidence','record':{
                'procedure_id':'record-worker-deadline','failure_shape':'worker-timeout',
                'source_event':event,'required_evidence':['elapsed_seconds','limit_seconds'],
                'invocation':'hermes kanban runs <task-id> --json'},'approval_required':False}}
    if attack in {'permission','budget','reviewer','other_profile','root_cause','markdown_policy'}:
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
    if attack == 'validator_failure':
        dispatch._record_task_failure(conn, validator.id, 'synthetic failure', outcome='crashed',
                                     release_claim=True, end_run=True, force_trip=True)
        assert kb.unblock_task(conn, validator.id)
        assert kb.claim_task(conn, validator.id) is None, 'Manual/native retry must not multiply validator reservations'
        assert conn.execute('SELECT status FROM workflow_lessons').fetchone()[0] == 'pending_approval'
        conn.close()
        return
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
    if attack in {'path_swap', 'hash_race'}:
        from hermes_cli import kanban_lesson_apply as apply
        original_replace = apply._replace
        outside = tmp_path / 'protected'
        outside.mkdir()
        victim = outside / reference.name
        victim.write_bytes(b'protected external bytes\n')
        concurrent_bytes = b'concurrent update must survive\n'
        def race(*args, **kwargs):
            if attack == 'path_swap':
                reference.parent.rename(reference.parent.with_name('retained-references'))
                reference.parent.symlink_to(outside, target_is_directory=True)
            else:
                reference.write_bytes(concurrent_bytes)
            return original_replace(*args, **kwargs)
        monkeypatch.setattr(apply, '_replace', race)
        dispatch.dispatch_once(conn, spawn_fn=lambda *a: None, max_spawn=0)
        assert victim.read_bytes() == b'protected external bytes\n'
        if attack == 'hash_race':
            assert reference.read_bytes() == concurrent_bytes
        assert conn.execute('SELECT status FROM workflow_lessons').fetchone()[0] == 'pending_approval'
        assert skill.read_bytes() == original_skill
        conn.close()
        return
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
    if attack == 'crash_after_replace':
        from hermes_cli import kanban_lesson_apply as apply
        original_replace = apply._replace
        def crash_after_replace(*args):
            original_replace(*args)
            raise SystemExit('synthetic crash after disk commit')
        with monkeypatch.context() as crash:
            crash.setattr(apply, '_replace', crash_after_replace)
            with pytest.raises(SystemExit, match='synthetic crash'):
                dispatch.dispatch_once(conn, spawn_fn=lambda *a: None, max_spawn=0)
        conn.close()
        conn = connect(tmp_path/'board.db')
        assert conn.execute('SELECT status FROM workflow_lesson_updates').fetchone()[0] == 'applying'
    if attack == 'concurrent':
        import os
        import subprocess
        import sys
        script = tmp_path / 'apply.py'
        script.write_text('''import json, sys
from pathlib import Path
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_lesson_apply import apply_next
with connect_closing(Path(sys.argv[1])) as conn:
    try:
        result = 'applied' if apply_next(conn, expected_hash=sys.argv[2]) else 'busy'
    except ValueError as exc:
        if 'stale' not in str(exc):
            raise
        result = 'stale'
print(json.dumps(result))
''')
        env = os.environ.copy()
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
        command = [sys.executable, str(script), str(tmp_path/'board.db'), hashlib.sha256(b'').hexdigest()]
        processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = []
        for process in processes:
            out, err = process.communicate(timeout=15)
            assert process.returncode == 0, err
            results.append(json.loads(out))
        assert results.count('applied') == 1
        assert set(results) <= {'applied', 'busy', 'stale'}
        stale = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert stale.returncode == 0 and json.loads(stale.stdout) == 'stale'
    dispatch.dispatch_once(conn,spawn_fn=lambda *a:None,max_spawn=0)
    assert reference.read_bytes(), 'Validated allowlisted evidence must actually reach the procedural reference'
    applied=dict(conn.execute('SELECT * FROM workflow_lessons').fetchone())
    assert applied['status']=='applied'
    assert applied['after_hash']==hashlib.sha256(reference.read_bytes()).hexdigest()
    assert len(reference.read_bytes().splitlines()) == 1, 'Crash/retry/concurrent apply cannot append twice'
    next_task=kb.create_task(conn,title='subsequent intake',assignee=None)
    assert lesson['id'] in kb.build_worker_context(conn,next_task)
    if attack == 'recurrence':
        assert kb.unblock_task(conn, owner)
        recurrence = kb.claim_task(conn, owner)
        assert recurrence is not None
        with write_txn(conn):
            kb._append_event(conn, owner, 'timed_out', {'elapsed_seconds': 12, 'limit_seconds': 10},
                             run_id=recurrence.current_run_id)
        later = conn.execute('SELECT * FROM workflow_incidents ORDER BY rowid DESC LIMIT 1').fetchone()
        assert later['prior_incident_id'] == incident['id']
        assert conn.execute('SELECT status FROM workflow_lessons WHERE id=?', (lesson['id'],)).fetchone()[0] == 'pending_approval'
        assert lesson['id'] not in kb.build_worker_context(conn, next_task)
        assert reference.read_bytes(), 'Reevaluation retains append-only audit evidence'
        assert later['lesson_status'] == 'pending_approval'
        conn.close()
        return
    from hermes_cli import kanban_lesson_apply as apply
    with pytest.raises(ValueError,match='stale'):
        apply.apply_next(conn,expected_hash=hashlib.sha256(b'').hexdigest())
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    config['kanban']['review_feedback']['auto_apply_lessons'] = False
    (home/'config.yaml').write_text(json.dumps(config))
    assert apply.rollback(conn,lesson['id'],expected_hash=applied['after_hash'],decision='synthetic rollback')
    assert reference.read_bytes()==b''
    assert hashlib.sha256(skill.read_bytes()).hexdigest()==config['kanban']['review_feedback']['protected_skill_hash']
    conn.close()
