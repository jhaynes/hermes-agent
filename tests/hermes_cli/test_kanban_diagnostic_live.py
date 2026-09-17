"""Real offline dispatcher/CLI diagnostic receipt and malicious-tool refusal."""
import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
import psutil

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect


@pytest.mark.parametrize('crash', [False, True, 'timeout'])
def test_reporter_and_validator_real_workers(tmp_path, monkeypatch, seed_review_worker_catalog, crash, record_property):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = home / 'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    root = Path(__file__).resolve().parents[2]
    assert Path(kb.__file__).resolve().is_relative_to(root)
    calls, errors, durations = [], [], []
    sentinel = tmp_path / 'must-not-exist'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if not request.get('model'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')
                return
            calls.append(request)
            tools = [m for m in request.get('messages', []) if m.get('role') == 'tool']
            text = json.dumps(request)
            matches = re.findall(r'work kanban task (t_[0-9a-f]+)', text)
            if not matches:
                errors.append('missing task identity')
                self.send_error(400)
                return
            task = matches[-1]
            if crash is True:
                from hermes_cli.kanban_db_connect import connect_closing
                with connect_closing(db) as control:
                    running = control.execute('SELECT worker_pid,worker_started_at FROM tasks WHERE id=?', (task,)).fetchone()
                    assert running['worker_pid'] and dispatch._worker_alive(running['worker_pid'], running['worker_started_at'])
                    os.kill(running['worker_pid'], signal.SIGKILL)
                self.close_connection = True
                return
            if crash == 'timeout':
                from hermes_cli.kanban_db_connect import connect_closing
                with connect_closing(db) as control:
                    with kb.write_txn(control):
                        control.execute('UPDATE workflow_postmortems SET active_seconds=601 WHERE task_id=?', (task,))
            if not tools:
                name, args = 'kanban_show', {}
            elif len(tools) == 1:
                name, args = 'write_file', {'path': str(sentinel), 'content': 'unauthorized'}
            else:
                try:
                    evidence = json.loads(tools[0]['content'])
                    event = evidence['evidence'][0]
                    if evidence['kind'] == 'validator':
                        replay = evidence['replay']
                        assert replay['elapsed_seconds'] >= replay['limit_seconds'] > 0
                    proposal = {'kind': 'procedural_evidence', 'approval_required': False, 'record': {
                        'procedure_id': 'record-worker-deadline', 'failure_shape': 'worker-timeout',
                        'source_event': event['id'], 'required_evidence': ['elapsed_seconds', 'limit_seconds'],
                        'invocation': 'hermes kanban runs <task-id> --json'}}
                    report = {'incident_id': evidence['incident_id'], 'owner': evidence['owner'],
                        'citations': [event['id']], 'facts': [event], 'hypotheses': [],
                        'confidence': 'unknown', 'contributing_conditions': [], 'missed_gates': [],
                        'confidence_basis': 'cause_not_established',
                        'recovery_recommendation': 'operator_investigation', 'proposed_change': proposal,
                        'validation_needed': ['deterministic_replay']}
                    metadata = ({'lesson_validation': {'source_event': evidence['source_event'], 'result': 'reproduced'}}
                                if evidence['kind'] == 'validator' else {'postmortem': report})
                    name, args = 'kanban_complete', {'summary': 'Synthetic receipt', 'metadata': metadata}
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append(str(exc))
                    self.send_error(400)
                    return
            call = {'id': 'call_' + str(len(tools)), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}
            chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 0,
                     'model': request['model'], 'choices': [{'index': 0, 'delta': {'role': 'assistant',
                     'tool_calls': [{'index': 0, **call}]}, 'finish_reason': None}]}
            final = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}
            data = ('data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(final) + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ('diagnostic', 'validator'):
        profile = home / 'profiles' / name
        profile.mkdir(parents=True)
        seed_review_worker_catalog(profile)
        (profile / 'cache' / 'openrouter_curated_catalog.json').write_text(json.dumps({
            'fetched_at': time.time(), 'curated': [['anthropic/claude-sonnet-4-5', 'fixture']]}))
        (profile / 'models_dev_cache.json').write_text(json.dumps({
            'openrouter': {'id': 'openrouter', 'name': 'Synthetic aggregator', 'models': {}}}))
        (profile / 'config.yaml').write_text(json.dumps({
            'model': {'provider': 'openrouter', 'default': 'anthropic/claude-sonnet-4-5',
                      'base_url': f'http://127.0.0.1:{server.server_port}/v1'},
            'agent': {'max_turns': 6}, 'memory': {'memory_enabled': False, 'user_profile_enabled': False}}))
        (profile / '.env').write_text('OPENROUTER_API_KEY=synthetic-local-only\n')
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n    validator_profile: validator\n')
    monkeypatch.setattr(dispatch, '_resolve_hermes_argv', lambda: [sys.executable, str(root / 'tests/hermes_cli/kanban_worker_probe.py'), str(root)])
    monkeypatch.setattr(dispatch, '_resolve_worker_cli_toolsets', lambda _: ['kanban', 'file', 'terminal', 'memory', 'delegation'])
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: p in {'diagnostic', 'validator'})
    conn = connect(db)
    owner = kb.create_task(conn, title='synthetic timeout', assignee=None)
    kb.block_task(conn, owner, kind='needs_input', reason='Synthetic operator hold')
    with kb.write_txn(conn):
        kb._append_event(conn, owner, 'timed_out', {'elapsed_seconds': 601, 'limit_seconds': 600})
    try:
        phases = ('timeout',) if crash == 'timeout' else ('crash_one', 'crash_retry') if crash else ('postmortem', 'validator')
        for kind in phases:
            started = time.monotonic()
            result = dispatch.dispatch_once(conn, max_spawn=1)
            assert len(result.spawned) == 1, (kind, [dict(r) for r in conn.execute('SELECT lesson_status,report FROM workflow_incidents')])
            task = result.spawned[0][0]
            until = time.monotonic() + 45
            while time.monotonic() < until and kb.get_task(conn, task).status == 'running':
                if crash:
                    dispatch.detect_crashed_workers(conn)
                if crash == 'timeout':
                    dispatch.enforce_max_runtime(conn)
                time.sleep(0.1)
            if crash == 'timeout':
                dispatch.enforce_max_runtime(conn)
            log = (kb.worker_logs_dir() / f'{task}.log').read_text()
            if not crash and kb.get_task(conn, task).status != 'done':
                print(log)
            assert kb.get_task(conn, task).status == ('blocked' if kind in {'crash_retry', 'timeout'} else 'ready' if crash else 'done'), (errors, log)
            durations.append(time.monotonic() - started)
            dispatch.reap_terminal_workers(conn)
        assert calls and not errors
        for request in calls:
            names = {t['function']['name'] for t in request.get('tools', [])}
            assert names == {'kanban_show', 'kanban_complete', 'kanban_heartbeat'}
        assert not sentinel.exists(), 'Unavailable tool calls must not regain file mutation authority'
        if crash:
            assert len(calls) == (1 if crash == 'timeout' else 2)
            assert conn.execute('SELECT runs_started FROM workflow_postmortems').fetchone()[0] == (1 if crash == 'timeout' else 2)
            assert conn.execute("SELECT report_status FROM workflow_incidents WHERE classification='failure'").fetchone()[0] == 'synthesis_failed'
            assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0] == 2
            assert not dispatch.dispatch_once(conn, max_spawn=1).spawned
        else:
            assert conn.execute('SELECT status FROM workflow_lessons').fetchone()[0] == 'validated'
        assert kb.get_task(conn, owner).status == 'blocked'
        record_property('diagnostic_pilot', json.dumps({
            'synthetic_endpoint': True, 'crash': crash, 'dispatch_to_terminal_seconds': durations,
            'model_request_count': len(calls), 'request_bytes': sum(len(json.dumps(c).encode()) for c in calls),
            'temporary_tree_bytes': sum(p.stat().st_size for p in tmp_path.rglob('*') if p.is_file()),
            'usage_note': 'No billed model usage; deterministic local fixture, not independent review.'}))
    finally:
        for task in kb.list_tasks(conn, status='running'):
            kb.reclaim_task(conn, task.id)
        dispatch.reap_terminal_workers(conn)
        for run in conn.execute('SELECT worker_pid,worker_started_at,claim_lock FROM task_runs WHERE worker_pid IS NOT NULL'):
            if dispatch._worker_alive(run['worker_pid'], run['worker_started_at']):
                result = dispatch._terminate_reclaimed_worker(run['worker_pid'], run['claim_lock'], started_at=run['worker_started_at'])
                assert result['terminated'], 'Synthetic diagnostic process must not survive fixture teardown'
        dispatch.reap_worker_zombies()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        conn.close()
