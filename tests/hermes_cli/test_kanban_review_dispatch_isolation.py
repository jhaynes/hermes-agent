"""One multiplexed dispatcher serves A→B→A through real source CLI workers."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.secret_scope import is_multiplex_active, set_multiplex_active
from gateway.run import _profile_runtime_scope
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect, write_txn
from tests.hermes_cli.review_readiness_helpers import writer_receipts


def test_real_dispatch_cli_endpoint_aba_preserves_original_history(tmp_path, monkeypatch, seed_review_worker_catalog, record_property):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = Path(__file__).resolve().parents[2]
    assert Path(kb.__file__).resolve().is_relative_to(root)
    homes = {name: tmp_path / '.hermes' / 'profiles' / name for name in ('a', 'b')}
    calls, errors, receipts = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            name = self.path.split('/')[1]
            calls.append((name, request))
            tools = [m for m in request.get('messages', []) if m.get('role') == 'tool']
            if not tools:
                tool, args = 'kanban_show', {}
            else:
                try:
                    evidence = json.loads(tools[0]['content'])
                    event = evidence['evidence'][0]
                    if evidence['kind'] == 'validator':
                        replay = evidence['replay']
                        assert replay['elapsed_seconds'] >= replay['limit_seconds'] > 0
                        metadata = {'lesson_validation': {'source_event': evidence['source_event'], 'result': 'reproduced'}}
                    else:
                        report = {'incident_id': evidence['incident_id'], 'owner': evidence['owner'],
                            'citations':[event['id']], 'facts':[event], 'hypotheses':[], 'confidence':'unknown',
                            'confidence_basis':'cause_not_established', 'contributing_conditions':[], 'missed_gates':[],
                            'recovery_recommendation':'operator_investigation', 'validation_needed':['deterministic_replay'],
                            'proposed_change':{'kind':'procedural_evidence','approval_required':False,'record':{
                                'procedure_id':'record-worker-deadline','failure_shape':'worker-timeout',
                                'source_event':event['id'],'required_evidence':['elapsed_seconds','limit_seconds'],
                                'invocation':'hermes kanban runs <task-id> --json'}}}
                        metadata = {'postmortem': report}
                    tool, args = 'kanban_complete', {'summary':'Synthetic isolated receipt','metadata':metadata}
                except (AssertionError, ValueError, KeyError, TypeError) as exc:
                    errors.append(str(exc))
                    self.send_error(400)
                    return
            call = {'id':'call_' + str(len(tools)), 'type':'function',
                    'function':{'name':tool,'arguments':json.dumps(args)}}
            chunk = {'id':'fixture','object':'chat.completion.chunk','created':0,'model':request['model'],
                'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0,**call}]},'finish_reason':None}]}
            final = {**chunk, 'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}
            data = ('data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(final)+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name, home in homes.items():
        for profile_name in (name, 'validator_' + name):
            profile = home.parent / profile_name
            profile.mkdir(parents=True)
            seed_review_worker_catalog(profile)
            (profile/'models_dev_cache.json').write_text(json.dumps({
                'openrouter':{'id':'openrouter','name':'Synthetic aggregator','models':{}}}))
            (profile/'cache'/'openrouter_curated_catalog.json').write_text(json.dumps({
                'fetched_at':time.time(),'curated':[['anthropic/claude-sonnet-4-5','fixture']]}))
            (profile/'config.yaml').write_text(json.dumps({
                'model':{'provider':'openrouter','default':'anthropic/claude-sonnet-4-5',
                         'base_url':f'http://127.0.0.1:{server.server_port}/{name}/v1'},
                'agent':{'max_turns':4}, 'memory':{'memory_enabled':False,'user_profile_enabled':False},
                'kanban':{'review_feedback':{'postmortem_profile':name,'validator_profile':'validator_' + name}}}))
            (profile/'.env').write_text('OPENROUTER_API_KEY=synthetic-local-only\n')
    monkeypatch.setattr(dispatch, '_resolve_hermes_argv', lambda:[sys.executable,str(root/'tests/hermes_cli/kanban_worker_probe.py'),str(root)])
    monkeypatch.setattr(dispatch, '_resolve_worker_cli_toolsets', lambda _:['kanban'])
    prior = is_multiplex_active()
    set_multiplex_active(True)
    snapshots = {}
    try:
        for name in ('a','b','a'):
            home = homes[name]
            db = home/'kanban.db'
            with monkeypatch.context() as local:
                local.setenv('HERMES_KANBAN_DB',str(db))
                with _profile_runtime_scope(home, hydrate_secrets=False):
                    conn = connect(db)
                    try:
                        if name not in snapshots:
                            # Colliding owner IDs make a board/profile leak observable.
                            with monkeypatch.context() as ids:
                                ids.setattr(kb,'_new_task_id',lambda:'t_11111111')
                                owner = kb.create_task(conn,title='isolated owner '+name,assignee=None)
                            attempt = state.enroll_review(conn,owner,expected_status='ready',expected_run_id=None,
                                board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
                                spec_digest=('a' if name=='a' else 'b')*64,base_sha='b'*40,target_sha='c'*40,
                                implementer_maker='openai',roster=sorted(state.REQUIRED_LANES),
                                consumed={'rounds':1 if name=='a' else 2,'recovery':1,'active_seconds':7},
                                compatibility=writer_receipts(conn),decision='synthetic retained history')
                            with write_txn(conn):
                                kb._append_event(conn,owner,'timed_out',{'elapsed_seconds':601,'limit_seconds':600},run_id=1)
                            snapshots[name] = {'attempt':state.get_attempt(conn,owner)}
                        else:
                            saved = snapshots[name]
                            assert state.get_attempt(conn,'t_11111111') == saved['attempt']
                            assert [dict(r) for r in conn.execute('SELECT * FROM workflow_incidents')] == saved['incidents']
                            assert [dict(r) for r in conn.execute('SELECT * FROM workflow_lessons')] == saved['lessons']
                        before = len(calls)
                        launched = dispatch.dispatch_once(conn,max_spawn=1)
                        assert len(launched.spawned)==1, (launched, [dict(r) for r in conn.execute('SELECT * FROM workflow_incidents')], kb.list_tasks(conn))
                        task = launched.spawned[0][0]
                        until = time.monotonic()+45
                        while time.monotonic()<until and kb.get_task(conn,task).status=='running':
                            time.sleep(0.1)
                        log = (kb.worker_logs_dir()/f'{task}.log').read_text()
                        assert kb.get_task(conn,task).status=='done', log
                        assert calls[before:] and all(scope==name for scope,_ in calls[before:])
                        assert 'source='+str(root/'hermes_cli/kanban_db.py') in log
                        assert state.get_attempt(conn,'t_11111111') == snapshots[name]['attempt']
                        incidents = [dict(r) for r in conn.execute('SELECT * FROM workflow_incidents')]
                        lessons = [dict(r) for r in conn.execute('SELECT * FROM workflow_lessons')]
                        assert len(incidents)==1 and len(lessons)==1
                        assert incidents[0]['board_id']==snapshots[name]['attempt']['board_id']
                        assert lessons[0]['incident_id']==incidents[0]['id']
                        snapshots[name].update(incidents=incidents,lessons=lessons)
                        receipts.append({'scope':name,'task':task,'run':kb.get_task(conn,task).current_run_id,
                                         'requests':len(calls)-before,'board':incidents[0]['board_id']})
                    finally:
                        for running in kb.list_tasks(conn,status='running'):
                            kb.reclaim_task(conn,running.id)
                        dispatch.reap_terminal_workers(conn)
                        conn.close()
        assert not errors
        assert snapshots['a']['attempt']['board_id'] != snapshots['b']['attempt']['board_id']
        assert snapshots['a']['incidents'][0]['id'] != snapshots['b']['incidents'][0]['id']
        assert snapshots['a']['lessons'][0]['id'] != snapshots['b']['lessons'][0]['id']
        assert snapshots['a']['lessons'][0]['status']=='validated'
        assert snapshots['b']['lessons'][0]['status']=='proposed'
        # Reopen B after returning to A: A's validator did not advance B's lesson.
        with connect(homes['b']/'kanban.db') as other:
            assert [dict(r) for r in other.execute('SELECT * FROM workflow_lessons')] == snapshots['b']['lessons']
        record_property('aba_worker_receipts',json.dumps(receipts))
    finally:
        set_multiplex_active(prior)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
