"""Real dispatcher → CLI worker → local model endpoint maker receipt."""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import psutil
import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_review_state as state
from hermes_cli import kanban_review_cohort as cohort
from hermes_cli.kanban_db_connect import connect


@pytest.mark.parametrize('implementer,revoke,phase', [
    ('openai',False,'review'), ('anthropic',False,'review'), ('openai',True,'review'),
    ('openai','identity','review'),
    ('openai','route','review'),
    ('anthropic',False,'preflight'), ('openai',False,'preflight'),
    ('openai',False,'review_launch_gap'), ('anthropic',False,'preflight_launch_gap'),
])
def test_normal_dispatch_worker_records_resolved_route(tmp_path, monkeypatch, implementer, revoke, phase, seed_review_worker_catalog):
    launch_gap = phase.endswith('_launch_gap')
    phase = phase.removesuffix('_launch_gap')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / '.hermes'
    profile = home / 'profiles' / 'reviewer'
    profile.mkdir(parents=True)
    seed_review_worker_catalog(profile)
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = home / 'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    calls = []
    request_paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if request.get('model'):
                calls.append(request)
                request_paths.append(self.path)
                if revoke and revoke != 'route' and len(calls) == 1:
                    from hermes_cli.kanban_db_connect import write_txn
                    with connect(db) as control:
                        with write_txn(control):
                            if revoke == 'identity':
                                control.execute('UPDATE workflow_board SET schema_version=99')
                            else:
                                state.hold(control, state.get_attempt(control, owner), 'synthetic_revocation')
            response = {'id':'synthetic','object':'chat.completion','created':0,
                        'model':request.get('model',''), 'choices':[{'index':0,'message':{'role':'assistant','content':'Synthetic model response.'},'finish_reason':'stop'}],
                        'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
            if revoke == 'route' and len(calls) == 1:
                response = {'error': {'message': 'Unsupported parameter: stream_options', 'type': 'invalid_request_error'}}
            data = json.dumps(response).encode()
            status = 400 if revoke == 'route' else 500
            self.send_response(status if revoke and len(calls) == 1 else 200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (profile / 'config.yaml').write_text(json.dumps({
        'model':{'provider':'openrouter','default':'anthropic/claude-sonnet-4-5','base_url':f'http://127.0.0.1:{server.server_port}/v1'},
        'agent':{'max_turns':1}, 'toolsets':[], 'memory':{'memory_enabled':False,'user_profile_enabled':False},
        'display':{'streaming':False}, 'terminal':{'backend':'local'},
    }))
    (profile / '.env').write_text('OPENROUTER_API_KEY=synthetic-local-only\n')
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
    git('init','-q')
    (repo/'probe.txt').write_text('isolated synthetic review target\n')
    git('add','probe.txt')
    git('-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-qm','fixture')
    sha = git('rev-parse','HEAD')
    conn = connect(db)
    owner = kb.create_task(conn,title='synthetic ask',assignee='reviewer' if phase=='preflight' else 'builder',
        workspace_kind='dir',workspace_path=str(repo),model_override='anthropic/claude-sonnet-4-5',provider_override='openrouter')
    attempt = state.enroll_review(conn,owner,expected_status='ready',expected_run_id=None,
        expected_assignee='reviewer' if phase == 'preflight' else 'builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],spec_digest='a'*64,
        base_sha=sha,target_sha=sha,implementer_maker=implementer,roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds':0,'recovery':0,'active_seconds':0},compatibility=writer_receipts(conn),decision='synthetic only')
    state.reserve_action(conn,owner,category='preflight',expected_version=0)
    if phase == 'review':
        run=kb.claim_task(conn,owner)
        assert run is not None
        cohort.record_runtime_route(conn, owner, run.current_run_id,
            provider='openai' if implementer == 'openai' else 'anthropic',
            model='gpt-5' if implementer == 'openai' else 'claude-sonnet-4-5', isolated=False)
        assert kb.request_review(conn,owner,expected_run_id=run.current_run_id)
    lanes={}
    for name in attempt['roster']:
        path=tmp_path/name
        git('worktree','add','--detach',str(path),sha)
        lanes[name]={'profile':'reviewer','model':'anthropic/claude-sonnet-4-5','provider':'openrouter','workspace':str(path)}
    if phase == 'review':
        cohort.start_cohort(conn,owner,lanes=lanes,expected_version=1)
    worker_argv = [sys.executable,str(root/'tests/hermes_cli/kanban_worker_probe.py'),str(root)]
    if revoke == 'route':
        worker_argv += ['--replace-request-route', f'http://127.0.0.1:{server.server_port}/forbidden/v1']
    monkeypatch.setattr(dispatch,'_resolve_hermes_argv',lambda:worker_argv)
    monkeypatch.setattr(dispatch,'_resolve_worker_cli_toolsets',lambda _:['kanban','delegation'])
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda p:p=='reviewer')
    interrupted = []
    if launch_gap:
        def interrupted_pid_write(connection, task_id, pid):
            interrupted.append((task_id, pid, dispatch._process_fingerprint(pid)))
            raise SystemExit('synthetic exit before launch identity persistence')
        monkeypatch.setattr(dispatch, '_set_worker_pid', interrupted_pid_write)
    try:
        if launch_gap:
            with pytest.raises(SystemExit, match='before launch identity'):
                dispatch.dispatch_once(conn, max_spawn=1)
            task_id, pid, fingerprint = interrupted[0]
            conn.close()
            conn = connect(db)
        else:
            result=dispatch.dispatch_once(conn,max_spawn=1)
            assert len(result.spawned)==1
            task_id=result.spawned[0][0]
            pid=kb.get_task(conn,task_id).worker_pid
        until=time.monotonic()+35
        route=None
        while time.monotonic()<until:
            if phase == 'review':
                route=conn.execute('SELECT * FROM review_members WHERE task_id=?',(task_id,)).fetchone()
            else:
                event=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='runtime_route_verified' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
                route=json.loads(event[0]) if event else None
            if calls and route and route['maker'] and not revoke:
                break
            if not psutil.pid_exists(pid) or psutil.Process(pid).status()==psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.1)
        refused = implementer == ('anthropic' if phase == 'review' else 'openai')
        if revoke == 'route':
            assert not calls, 'Client replacement after preparation must send zero requests'
            log=(kb.worker_logs_dir()/f'{task_id}.log').read_text()
            assert 'managed transport route changed after admission' in log
        elif refused:
            assert not calls, 'Same-maker worker must send zero model requests'
            assert route is None or route['maker'] is None
            log=(kb.worker_logs_dir()/f'{task_id}.log').read_text()
            assert ('unverified or non-independent reviewer route' if phase=='review' else 'implementer route differs') in log
        else:
            assert calls, ('Real CLI worker did not reach the local endpoint',
                           (kb.worker_logs_dir()/f'{task_id}.log').read_text())
            assert route is not None, 'Actual implementer routing must be recorded, not just declared at enrollment'
            assert route['maker']=='anthropic', 'Actual worker construction must persist resolved maker before provider request'
            assert route['run_id']==kb.get_task(conn,task_id).current_run_id
            if phase == 'review':
                names = {tool['function']['name'] for tool in calls[0].get('tools', [])}
                assert 'delegate_task' not in names, 'Reviewer construction must prohibit nested review workers'
            if revoke:
                assert not any('/forbidden/' in path for path in request_paths), 'Transport reconnect/client replacement must send zero requests to an unreserved endpoint'
                assert len(calls) == 1, 'SDK retry must recheck authorization at the actual HTTP send'
            if launch_gap:
                saved = conn.execute('SELECT worker_pid,worker_started_at FROM tasks WHERE id=?', (task_id,)).fetchone()
                assert tuple(saved) == (pid, fingerprint), 'Managed child must adopt the already-spent launch identity'
                assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0] == 1
                assert not dispatch.dispatch_once(conn, max_spawn=0).spawned
    finally:
        # Stop only the worker started by this test, preserving fingerprint checks.
        for task in kb.list_tasks(conn,status='running'):
            if task.id != owner or phase == 'preflight':
                kb.reclaim_task(conn,task.id)
        dispatch.reap_terminal_workers(conn)
        for _, child, started in interrupted:
            if dispatch._worker_alive(child, started):
                psutil.Process(child).terminate()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        conn.close()
