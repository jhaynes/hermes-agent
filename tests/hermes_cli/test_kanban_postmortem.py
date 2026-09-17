"""Diagnostic dispatch is bounded and cannot create recursive incident work."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect


def test_reporter_uses_existing_queue_and_failure_cannot_recurse(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: p == 'diagnostic')
    conn = connect(tmp_path / 'board.db')
    owner = kb.create_task(conn, title='blocked implementation', assignee=None)
    assert kb.block_task(conn, owner, kind='capability', reason='synthetic failure')
    spawned = []
    dispatch.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t), max_spawn=1)
    assert len(spawned) == 1, 'A genuine failure must enqueue bounded durable diagnostic work'
    reporter = spawned[0]
    assert reporter.id != owner
    assert reporter.assignee == 'diagnostic'
    assert reporter.max_runtime_seconds <= 600
    for _ in range(2):
        dispatch._record_task_failure(conn, reporter.id, 'synthesis process failed', outcome='spawn_failed',
                                     failure_limit=100, release_claim=True, end_run=True)
        dispatch.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t), max_spawn=1)
    assert len(spawned) == 2
    incidents = [dict(r) for r in conn.execute('SELECT * FROM workflow_incidents')]
    assert len(incidents) == 1
    assert incidents[0]['task_id'] == owner
    assert incidents[0]['report_status'] == 'synthesis_failed'
    assert kb.get_task(conn, owner).status == 'blocked'
    assert kb.claim_task(conn, reporter.id) is None
    conn.close()


def test_completed_report_is_cited_redacted_and_attached_without_resolving_owner(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    home=tmp_path/'home'
    home.mkdir()
    (home/'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda p:p=='diagnostic')
    conn=connect(tmp_path/'board.db')
    owner=kb.create_task(conn,title='failure owner',assignee=None)
    assert kb.block_task(conn,owner,kind='capability',reason='synthetic failure')
    spawned=[]
    dispatch.dispatch_once(conn,spawn_fn=lambda t,w:spawned.append(t),max_spawn=1)
    reporter=spawned[0]
    incident=dict(conn.execute('SELECT * FROM workflow_incidents').fetchone())
    report={'incident_id':incident['id'],'citations':json.loads(incident['source_events']),
            'facts':['A capability block occurred. api_key=super-secret-abc123'],
            'hypotheses':['Cause not established.'],'confidence':'unknown',
            'contributing_conditions':[],'missed_gates':[],'recovery_recommendation':'Operator investigation required.',
            'proposed_change':None,'validation_needed':['Independent reproduction'], 'owner':owner}
    assert kb.complete_task(conn,reporter.id,expected_run_id=reporter.current_run_id,
                            metadata={'postmortem':report})
    dispatch.dispatch_once(conn,spawn_fn=lambda *a:None,max_spawn=0)
    updated=dict(conn.execute('SELECT * FROM workflow_incidents').fetchone())
    assert updated['report_status']=='complete', 'A completed diagnostic needs a validated durable report'
    assert updated['state']=='open'
    attachments=kb.list_attachments(conn,owner)
    assert len(attachments)==1
    artifact=Path(attachments[0].stored_path).read_text()
    assert 'super-secret-abc123' not in artifact
    assert 'super-secret-abc123' not in json.dumps([dict(r) for r in conn.execute('SELECT * FROM task_runs')])
    assert json.loads(artifact)['citations']==report['citations']
    assert kb.get_task(conn,owner).status=='blocked'
    dispatch.dispatch_once(conn,spawn_fn=lambda *a:None,max_spawn=0)
    assert len(kb.list_attachments(conn,owner))==1
    conn.close()
