"""Synthetic whole-cohort pilot using real CLI workers and executed probes.

The local endpoint is a deterministic tool-call fixture, not independent review
or a live-provider attestation. Every finding is derived from actual tool output.
"""
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
from tests.hermes_cli.review_readiness_helpers import writer_receipts

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_review_state as state
from hermes_cli import kanban_review_cohort as cohort
from hermes_cli.kanban_db_connect import connect, connect_closing


PROBE = '''import importlib.util, json, pathlib, sys, subprocess
mandate, root = sys.argv[1], pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location('subject', root / 'subject.py')
m = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(m)
findings = []
def check(condition, evidence, change):
    if not condition:
        findings.append({'severity': 'high', 'location': {'path': 'subject.py', 'line': 1},
                         'evidence': {'kind': 'executed', 'command': 'fixture probe ' + mandate, 'result': evidence}, 'required_change': change})
if mandate == 'breaker_a':
    try:
        m.count('-1')
    except ValueError:
        rejected = True
    else:
        rejected = False
    check(rejected, 'Executed count(-1); negative input was accepted', 'Reject negative counts')
elif mandate == 'breaker_b':
    class DelayedDependency:
        def __int__(self):
            raise TimeoutError('synthetic unavailable dependency')
    try:
        m.count(DelayedDependency())
    except TimeoutError:
        pass
    else:
        raise AssertionError('Dependency timeout was swallowed')
    assert m.count('7') == 7
    print('Executed dependency timeout and subsequent call; fixture has no concurrency')
elif mandate == 'breaker_c':
    try:
        m.count('invalid')
    except ValueError:
        pass
    assert m.count('2') == 2
    base = subprocess.check_output(['git', '-C', str(root), 'rev-list', '--max-parents=0', 'HEAD'], text=True).strip()
    old = subprocess.check_output(['git', '-C', str(root), 'show', base + ':subject.py'], text=True)
    restored = {}
    exec(compile(old, '<isolated rollback snapshot>', 'exec'), restored)
    assert restored['count']('2') == m.count('2') == 2
    print('Executed failure/retry and isolated prior-version rollback interoperability')
elif mandate == 'scope':
    check(not (root / 'unrequested.txt').exists(), 'Inspected full fixture tree: unrequested.txt exists', 'Remove unrequested file')
else:
    assert m.count('0') == 0
    assert m.count('12') == 12
    print('Executed specialist baseline probe: ' + mandate)
print('PILOT_RESULT=' + json.dumps({'mandate': mandate, 'findings': findings}))
'''


@pytest.mark.parametrize('clean_third', [True, False])
def test_real_cohort_repair_scope_trim_and_clean_third_round(tmp_path, monkeypatch, clean_third, record_property, seed_review_worker_catalog):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / '.hermes'
    profile = home / 'profiles' / 'reviewer'
    profile.mkdir(parents=True)
    seed_review_worker_catalog(profile)
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = home / 'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / 'probe.py'
    probe.write_text(PROBE)
    cards, steps, attacks, errors = {}, {}, [], []
    queue_seconds, request_bytes = [], []
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
            request_bytes.append(len(json.dumps(request).encode()))
            text = '\n'.join(str(m.get('content', '')) for m in request.get('messages', []))
            matches = re.findall(r'work kanban task (t_[0-9a-f]+)', text)
            task = matches[-1] if matches else None
            if task not in cards:
                errors.append('missing canonical task context')
                self.send_error(400)
                return
            card = cards[task]
            step = steps.get(task, 0)
            steps[task] = step + 1
            if step == 0:
                name, arguments = 'kanban_show', {'task_id': task}
            elif card['mandate'] == 'preflight':
                name, arguments = 'kanban_request_review', {'summary': 'Synthetic preflight complete'}
            elif step == 1:
                command = shlex.join([sys.executable, str(probe), card['mandate'], card['workspace']])
                name, arguments = 'terminal', {'command': command, 'timeout': 10}
            else:
                results = re.findall(r'PILOT_RESULT=(\{[^\n]+)', text)
                if not results:
                    errors.append('no executed probe receipt for ' + task)
                    self.send_error(400)
                    return
                # Tool messages serialize terminal stdout inside a JSON envelope.
                result = None
                for message in request['messages']:
                    if message.get('role') != 'tool':
                        continue
                    try:
                        payload = json.loads(message.get('content', ''))
                        output = payload.get('output', '')
                        line = next((line for line in output.splitlines() if line.startswith('PILOT_RESULT=')), None)
                        if line:
                            result = json.loads(line.removeprefix('PILOT_RESULT='))
                    except (ValueError, TypeError):
                        pass
                if result is None:
                    errors.append('probe output could not be decoded')
                    self.send_error(400)
                    return
                attacks.append((task, result['mandate'], result['findings']))
                with connect_closing(db) as control:
                    attempt = state.get_attempt(control, task)
                    member = control.execute('SELECT * FROM review_members WHERE task_id=?', (task,)).fetchone()
                    run_id = kb.get_task(control, task).current_run_id
                    receipt = {k: attempt[k] for k in ('board_id', 'base_sha', 'target_sha', 'policy_digest', 'spec_digest')}
                    prior = cohort.prior_findings(control, attempt['id'], member['mandate'])
                receipt.update(attempt_id=attempt['id'], round_id=member['round_id'], mandate=member['mandate'],
                    task_id=task, run_id=run_id, verdict='request_changes' if result['findings'] else 'approve',
                    findings=result['findings'], verification_run=[{'kind':'executed', 'command':'fixture probe ' + member['mandate'], 'result':json.dumps(result)}],
                    prior_findings=[{'finding_id': f['finding_id'], 'status': 'open' if result['findings'] else 'closed',
                                     'evidence': {'kind':'executed', 'command':'fixture probe ' + member['mandate'], 'result':json.dumps(result)}} for f in prior])
                if member['mandate'] == 'scope':
                    receipt['scope'] = {
                        'mapping': [{'requirement': 'Reject negative counts; no additional files',
                                     'change': 'subject.py negative-input validation and full fixture tree',
                                     'evidence': receipt['verification_run'][0]}],
                        'missing_evidence': [],
                        'extraneous': ['unrequested.txt'] if result['findings'] else [],
                        'safety_dispositions': [{'change': 'negative-input ValueError',
                            'requirement': 'Reject negative counts', 'rationale': 'Input rejection is the requested safety correction.',
                            'disposition': 'necessary_safety', 'follow_up': None}],
                    }
                name, arguments = 'kanban_complete', {'summary': 'Executed isolated fixture probe', 'metadata': {'bounded_review': receipt}}
            call = {'id': 'call_' + str(step), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)}}
            if request.get('stream'):
                chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 0,
                         'model': request['model'], 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, **call}]}, 'finish_reason': None}]}
                final = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
                data = ('data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(final) + '\n\ndata: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
            else:
                data = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 0,
                    'model': request['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [call]}, 'finish_reason': 'tool_calls'}]}).encode()
                content_type = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (profile / 'config.yaml').write_text(json.dumps({
        'model': {'provider': 'openrouter', 'default': 'anthropic/claude-sonnet-4-5', 'base_url': f'http://127.0.0.1:{server.server_port}/v1'},
        'agent': {'max_turns': 5}, 'platform_toolsets': {'cli': ['terminal', 'kanban']},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'terminal': {'backend': 'local'},
    }))
    (profile / '.env').write_text('OPENROUTER_API_KEY=synthetic-local-only\n')
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
    def commit():
        git('add', '.')
        git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'fixture')
        return git('rev-parse', 'HEAD')
    git('init', '-q')
    (repo / 'subject.py').write_text('def count(value):\n    return int(value)\n')
    (repo / 'unrequested.txt').write_text('outside the ask\n')
    sha = base = commit()
    conn = connect(db)
    owner = kb.create_task(conn, title='Reject negative counts; no additional files', assignee='reviewer',
        workspace_kind='dir', workspace_path=str(repo), model_override='openai/gpt-5', provider_override='openrouter')
    attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None, expected_assignee='reviewer',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0], spec_digest='a'*64,
        base_sha=base, target_sha=sha, implementer_maker='openai', roster=sorted(state.REQUIRED_LANES | {'docs', 'system'}),
        consumed={'rounds': 0, 'recovery': 0, 'active_seconds': 0},
        compatibility=writer_receipts(conn), decision='synthetic pilot')
    monkeypatch.setattr(dispatch, '_resolve_hermes_argv', lambda: [sys.executable, str(root / 'tests/hermes_cli/kanban_worker_probe.py'), str(root)])
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: p == 'reviewer')
    try:
        for ordinal in range(1, 4):
            current = state.get_attempt(conn, owner)
            state.reserve_action(conn, owner, category='preflight', expected_version=current['version'])
            cards[owner] = {'mandate': 'preflight'}
            steps[owner] = 0
            result = dispatch.dispatch_once(conn, max_spawn=1)
            assert len(result.spawned) == 1 and result.spawned[0][0] == owner
            until = time.monotonic() + 35
            from hermes_cli.kanban_review_guards import has_live_worker
            while time.monotonic() < until:
                dispatch.reap_terminal_workers(conn)
                if kb.get_task(conn, owner).status == 'review' and not has_live_worker(conn, attempt['id']):
                    break
                time.sleep(0.1)
            assert kb.get_task(conn, owner).status == 'review', (kb.worker_logs_dir() / f'{owner}.log').read_text()
            assert not has_live_worker(conn, attempt['id'])
            lanes = {}
            for mandate in attempt['roster']:
                workspace = tmp_path / f'{ordinal}-{mandate}'
                git('worktree', 'add', '--detach', str(workspace), sha)
                lanes[mandate] = {'profile': 'reviewer', 'provider': 'openrouter', 'model': 'anthropic/claude-sonnet-4-5', 'workspace': str(workspace)}
            round_id = cohort.start_cohort(conn, owner, lanes=lanes, expected_version=state.get_attempt(conn, owner)['version'])
            reserved_at = time.monotonic()
            members = conn.execute('SELECT * FROM review_members WHERE round_id=?', (round_id,)).fetchall()
            for member in members:
                cards[member['task_id']] = {**dict(member), 'workspace': lanes[member['mandate']]['workspace']}
            for _ in members:
                result = dispatch.dispatch_once(conn, max_spawn=1)
                queue_seconds.append(time.monotonic() - reserved_at)
                assert len(result.spawned) == 1, (result, errors)
                task = result.spawned[0][0]
                until = time.monotonic() + 35
                while time.monotonic() < until and kb.get_task(conn, task).status == 'running':
                    time.sleep(0.1)
                assert kb.get_task(conn, task).status == 'done', (errors, (kb.worker_logs_dir() / f'{task}.log').read_text()[-6000:])
                dispatch.reap_terminal_workers(conn)
                from hermes_cli.kanban_review_guards import has_live_worker
                until = time.monotonic() + 8
                while has_live_worker(conn, attempt['id']) and time.monotonic() < until:
                    time.sleep(0.1)
                    dispatch.reap_terminal_workers(conn)
                assert not has_live_worker(conn, attempt['id']), 'Do not treat a released claim as physical quiescence'
            current = state.get_attempt(conn, owner)
            assert current['completed_rounds'] == ordinal
            if ordinal == 3:
                assert current['state'] == ('approved' if clean_third else 'held')
                assert kb.complete_task(conn, owner, summary='Attempted completion', force=True) is clean_third
                assert cohort.start_cohort(conn, owner, lanes=lanes, expected_version=current['version']) is None
                assert not dispatch.dispatch_once(conn, max_spawn=1).spawned
                break
            assert current['state'] == 'repair'
            state.reserve_action(conn, owner, category='repair', expected_version=current['version'])
            repair = kb.claim_task(conn, owner)
            if ordinal == 1:
                (repo / 'subject.py').write_text('def count(value):\n    result = int(value)\n    if result < 0:\n        raise ValueError("negative")\n    return result\n')
            elif clean_third:
                (repo / 'unrequested.txt').unlink()
            else:
                (repo / 'subject.py').write_text((repo / 'subject.py').read_text() + '\n# Scope finding deliberately remains for cap rejection.\n')
            sha = commit()
            assert kb.request_review(conn, owner, expected_run_id=repair.current_run_id, metadata={'bounded_review': {'target_sha': sha}})
        assert {mandate for _, mandate, _ in attacks} == set(attempt['roster'])
        assert not errors
        record_property('pilot_metrics', json.dumps({
            'model_requests': len(request_bytes), 'request_bytes': sum(request_bytes),
            'queue_seconds': queue_seconds,
            'workspace_bytes': sum(p.stat().st_size for p in tmp_path.rglob('*') if p.is_file()),
            'completed_rounds': current['completed_rounds'], 'recovery_used': current['recovery_used'],
            'usage_source': 'local deterministic endpoint; fixture tokens are not real model billing',
            'clean_third': clean_third,
        }, sort_keys=True))
    finally:
        for task in kb.list_tasks(conn, status='running'):
            kb.reclaim_task(conn, task.id)
        dispatch.reap_terminal_workers(conn)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        conn.close()
