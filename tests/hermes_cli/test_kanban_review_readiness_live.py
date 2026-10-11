"""Actual CLI processes and serving gateway/dashboard transports in a temp home."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from gateway.control_socket import query_gateway_control
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_review_state as state
from tests.hermes_cli.test_kanban_review_readiness import enrollment


def test_actual_writer_processes_and_dead_writer_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    root = Path(__file__).resolve().parents[2]
    home = tmp_path/'home'
    home.mkdir()
    path = home/'board.db'
    env = {**os.environ, 'HERMES_HOME':str(home), 'HERMES_KANBAN_DB':str(path),
           'PYTHONPATH':os.pathsep.join([str(root), os.environ.get('PYTHONPATH','')])}
    ready, stop = tmp_path/'ready.json', tmp_path/'stop'
    log = (tmp_path/'server.log').open('w+')
    server = subprocess.Popen([sys.executable,str(root/'tests/hermes_cli/kanban_readiness_probe.py'),
                               str(root),str(home),str(ready),str(stop)],env=env,stdout=log,stderr=log)
    try:
        until = time.monotonic()+30
        while not ready.exists() and server.poll() is None and time.monotonic()<until:
            time.sleep(.02)
        log.flush()
        assert ready.exists(), (tmp_path/'server.log').read_text()
        with connect(path) as conn:
            task = kb.create_task(conn,title='actual writer adoption',assignee='builder')
            payload = tmp_path/'receipt.json'
            payload.write_text(json.dumps({'operation':'readiness'}))
            command = [sys.executable,str(root/'hermes'),'kanban','enroll-review',task,'--receipt',str(payload)]
            cli = subprocess.run(command,env=env,text=True,capture_output=True,timeout=30)
            assert cli.returncode == 0, cli.stderr
            issued = json.loads(cli.stdout)
            selection = {'challenge_id':issued['challenge_id'],'cli':issued['id']}
            response = query_gateway_control(home,'review-readiness',params={'challenge_id':issued['challenge_id']})
            assert response and response['surface'] == 'gateway', response
            selection['gateway'] = response['id']
            port = json.loads(ready.read_text())['port']
            response = httpx.post(f'http://127.0.0.1:{port}/review-readiness',json={'challenge_id':issued['challenge_id']})
            assert response.status_code == 200, response.text
            selection['dashboard'] = response.json()['id']
            payload.write_text(json.dumps({**enrollment(conn),'compatibility':selection}))
            result = subprocess.run(command,env=env,text=True,capture_output=True,timeout=30)
            assert result.returncode == 0, result.stderr
            assert state.get_attempt(conn,task)['compatibility']['writers']['gateway']['pid'] == server.pid
            # A new owner cannot rely on a once-live server after it exits.
            other = kb.create_task(conn,title='next owner',assignee='builder')
            stop.touch()
            assert server.wait(timeout=10) == 0
            values = enrollment(conn)
            values['spec_digest'] = 'd'*64
            payload.write_text(json.dumps({**values,'compatibility':selection}))
            command[4] = other
            denied = subprocess.run(command,env=env,text=True,capture_output=True,timeout=30)
            assert denied.returncode != 0 and 'stale readiness writer' in denied.stderr
            assert state.get_attempt(conn,other) is None
    finally:
        stop.touch()
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        log.close()
