"""Shared Responses/summary sends must honor the same live admission as turns."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI, APIConnectionError
from anthropic import Anthropic, APIConnectionError as AnthropicConnectionError
from agent.client_lifecycle import ClientLifecycleMixin
from agent.chat_completion_helpers import _anthropic_summary_attempt

from agent.codex_runtime import run_codex_stream
from agent.chat_completion_helpers import _codex_summary_attempt, _chat_summary_attempt
from hermes_cli import kanban_db as kb, kanban_review_state as state
from hermes_cli.kanban_db_connect import connect, write_txn
from hermes_cli.kanban_review_worker import before_model_request
from tests.hermes_cli.review_readiness_helpers import writer_receipts


@pytest.mark.parametrize('mode,change', [
    (mode, change)
    for mode in ('direct', 'codex_summary', 'chat_summary', 'anthropic_summary')
    for change in ('hold', 'deadline', 'endpoint', 'sdk_retry', 'reconnect_hold', 'reconnect_endpoint')
    if mode in ('direct', 'codex_summary') or not change.startswith('reconnect_')
])
def test_shared_native_send_rechecks_admission(tmp_path, monkeypatch, mode, change):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    db = tmp_path / 'board.db'
    conn = connect(db)
    task = kb.create_task(conn, title='synthetic native send', assignee='builder')
    state.enroll_review(conn, task, expected_status='ready', expected_run_id=None,
        expected_assignee='builder', board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=sorted(state.REQUIRED_LANES), consumed={'rounds':0,'recovery':0,'active_seconds':0},
        compatibility=writer_receipts(conn), decision='synthetic operator')
    state.reserve_action(conn, task, category='preflight', expected_version=0)
    run = kb.claim_task(conn, task)
    assert run is not None
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run.current_run_id))
    sent = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            sent.append(self.path)
            reconnect = change.startswith('reconnect_') and len(sent) == 2
            if reconnect:
                if change == 'reconnect_hold':
                    with connect(db) as control:
                        with write_txn(control):
                            state.hold(control, state.get_attempt(control, task), 'synthetic_reconnect_hold')
                else:
                    client.base_url = endpoint + '/forbidden/v1'
            if mode == 'chat_summary':
                data = json.dumps({'id':'synthetic','object':'chat.completion','created':0,
                    'model':'gpt-5','choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]}).encode()
            elif mode == 'anthropic_summary':
                data = json.dumps({'id':'synthetic', 'type':'message', 'role':'assistant',
                    'model':'gpt-5', 'content':[{'type':'text', 'text':'ok'}],
                    'stop_reason':'end_turn', 'usage':{'input_tokens':1, 'output_tokens':1}}).encode()
            else:
                event = {'type':'response.completed', 'response':{'id':'synthetic','status':'completed','output':[]}}
                data = ('data: '+json.dumps(event)+'\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if mode in ('chat_summary', 'anthropic_summary') else 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if not reconnect:
                self.wfile.write(data)
            # A truncated body drives the real Responses reconnect, not an SDK mock.
            self.close_connection = True

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    client_type = Anthropic if mode == 'anthropic_summary' else OpenAI
    client = client_type(api_key='synthetic', base_url=endpoint + '/v1', max_retries=0,
                         http_client=httpx.Client(trust_env=False))
    agent = SimpleNamespace(provider='openai', model='gpt-5', base_url=str(client.base_url),
        api_mode='chat_completions' if mode == 'chat_summary' else 'codex_responses',
        max_iterations=2, _interrupt_requested=False, session_id='', _force_ascii_payload=False,
        _ensure_primary_openai_client=lambda **kw:client, _client_log_context=lambda:'synthetic',
        _touch_activity=lambda *a:None, _fire_stream_delta=lambda *a:None,
        _fire_reasoning_delta=lambda *a:None,
        _buffer_diagnostic_status=lambda *a:None,
        _build_api_kwargs=lambda messages:{'model':'gpt-5', 'messages':messages} if mode == 'chat_summary' else {'model':'gpt-5','input':messages},
        _get_transport=lambda:SimpleNamespace(normalize_response=lambda response, **kw:SimpleNamespace(content='ok', tool_calls=[])))
    agent._run_codex_stream = lambda kwargs:run_codex_stream(agent, kwargs)
    if mode == 'anthropic_summary':
        agent.api_mode = 'anthropic_messages'
        agent._anthropic_client = client
        agent._try_refresh_anthropic_client_credentials = lambda: None
        agent._capture_anthropic_response_headers = lambda *a, **kw: None
        agent._disable_streaming = True
        agent.max_tokens = 32
        agent.reasoning_config = None
        agent._is_anthropic_oauth = False
        agent._anthropic_preserve_dots = lambda: False
        agent._anthropic_messages_create = lambda kwargs: ClientLifecycleMixin._anthropic_messages_create(agent, kwargs)
        agent._get_transport = lambda: SimpleNamespace(
            build_kwargs=lambda **kw: {'model': agent.model, 'max_tokens':32, 'messages':[]},
            normalize_response=lambda response, **kw: SimpleNamespace(content='ok', tool_calls=[]))
    before_model_request(agent, {'model':'gpt-5'})
    builders = {'codex_summary': _codex_summary_attempt, 'chat_summary': _chat_summary_attempt,
                'anthropic_summary': _anthropic_summary_attempt}
    invoke = (lambda:run_codex_stream(agent, {'model':'gpt-5','input':[]})) if mode == 'direct' else builders[mode](agent, [], 'synthetic-summary')
    call = invoke if mode == 'direct' else lambda:invoke(0)
    try:
        call()
        assert len(sent) == 1, 'The admitted route must actually work before revocation'
        if change == 'hold':
            with write_txn(conn):
                state.hold(conn, state.get_attempt(conn, task), 'synthetic_hold')
        elif change == 'deadline':
            with write_txn(conn):
                conn.execute('UPDATE review_actions SET deadline=0 WHERE task_id=?', (task,))
        elif change == 'endpoint':
            client.base_url = endpoint + '/forbidden/v1'
        elif change == 'sdk_retry':
            client.max_retries = 2
        try:
            call()
        except (PermissionError, TimeoutError, APIConnectionError, AnthropicConnectionError):
            pass
        assert len(sent) == (2 if change.startswith('reconnect_') else 1), 'A shared client must not bypass admission at a later physical send'
        assert not any('/forbidden/' in path for path in sent)
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        conn.close()
