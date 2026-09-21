"""Writer readiness is produced by the writers, not supplied as version prose."""
import argparse
import json
import pytest
from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.review_readiness_helpers import writer_receipts


def enrollment(conn):
    return dict(expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40,
        implementer_maker='openai', roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds':0,'recovery':0,'active_seconds':0}, decision='synthetic operator')


def test_declared_versions_are_not_writer_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with connect(tmp_path/'board.db') as conn:
        task = kb.create_task(conn, title='synthetic ask', assignee='builder')
        with pytest.raises(ValueError, match='readiness'):
            state.enroll_review(conn, task, **enrollment(conn),
                                compatibility={'cli':1,'gateway':1,'dashboard':1})
        assert state.get_attempt(conn, task) is None
        assert kb.get_task(conn, task).status == 'ready'


def test_supported_writers_issue_board_bound_receipts(tmp_path, monkeypatch, capsys):
    from gateway.control_socket import GatewayControlServer
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router

    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='synthetic ask', assignee='builder')
        receipt_file = tmp_path/'receipt.json'
        receipt_file.write_text(json.dumps({'operation':'readiness'}))
        parser = argparse.ArgumentParser()
        cli.build_parser(parser.add_subparsers(dest='command'))
        args = parser.parse_args(['kanban','enroll-review',task,'--receipt',str(receipt_file)])
        assert cli.kanban_command(args) == 0
        issued = json.loads(capsys.readouterr().out)
        selection = {'challenge_id':issued['challenge_id'], 'cli':issued['id']}
        control = GatewayControlServer(home=tmp_path)
        response = json.loads(control.handle_request_line(json.dumps({
            'verb':'review-readiness', 'params':{'challenge_id':issued['challenge_id']}
        }).encode()))
        assert response['ok'], response
        selection['gateway'] = response['result']['id']
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.post('/review-readiness', json={'challenge_id':issued['challenge_id']})
        assert response.status_code == 200, response.text
        selection['dashboard'] = response.json()['id']
        receipt_file.write_text(json.dumps({**enrollment(conn), 'compatibility':selection}))
        assert cli.kanban_command(args) == 0
        after = state.get_attempt(conn, task)
        assert after['state'] == 'preflight'
        assert after['compatibility']['selection'] == selection
        assert {r['board_id'] for r in after['compatibility']['writers'].values()} == {after['board_id']}


@pytest.mark.parametrize('fault', ['missing','stale','foreign','unknown','mixed','dead'])
def test_readiness_refuses_before_enrollment_mutation(tmp_path, monkeypatch, fault):
    from hermes_cli import kanban_review_readiness as readiness
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with connect(tmp_path/'board.db') as conn:
        task = kb.create_task(conn, title='synthetic ask', assignee='builder')
        selection = writer_receipts(conn)
        if fault == 'missing':
            selection.pop('gateway')
        elif fault == 'stale':
            readiness.begin(conn)
        elif fault == 'foreign':
            with connect(tmp_path/'other.db') as foreign:
                selection = writer_receipts(foreign)
        else:
            row = conn.execute('SELECT receipt FROM review_writer_receipts WHERE id=?', (selection['gateway'],)).fetchone()
            receipt = json.loads(row[0])
            key, value = {'unknown':('protocol',99), 'mixed':('runtime_digest','0'*64),
                          'dead':('started_at',0)}[fault]
            receipt[key] = value
            conn.execute('UPDATE review_writer_receipts SET receipt=? WHERE id=?',
                         (json.dumps(receipt), selection['gateway']))
            conn.commit()
        with pytest.raises(ValueError, match='readiness'):
            state.enroll_review(conn, task, **enrollment(conn), compatibility=selection)
        assert state.get_attempt(conn, task) is None
        assert kb.get_task(conn, task).status == 'ready'


def test_changed_runtime_cannot_claim_managed_work(tmp_path, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_review_readiness as readiness
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with connect(tmp_path/'board.db') as conn:
        task = kb.create_task(conn, title='synthetic ask', assignee='builder')
        state.enroll_review(conn, task, **enrollment(conn), compatibility=writer_receipts(conn))
        state.reserve_action(conn, task, category='preflight', expected_version=0)
        monkeypatch.setattr(readiness, 'runtime_digest', lambda:'different-loaded-runtime')
        assert kb.claim_task(conn, task) is None
        assert state.get_attempt(conn, task)['state'] == 'held'
        with pytest.raises(sqlite3.DatabaseError, match='mixed-version'):
            kb.assign_task(conn, task, 'different-owner')
        assert kb.get_task(conn, task).assignee == 'builder'
        with pytest.raises(sqlite3.DatabaseError, match='mixed-version'):
            conn.execute('UPDATE tasks SET block_kind=NULL WHERE id=?',(task,))


def test_successor_requires_current_writer_readiness(tmp_path, monkeypatch):
    from hermes_cli import kanban_review_readiness as readiness
    from tests.hermes_cli.test_kanban_review_operator import invoke
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    with connect(path) as conn:
        task = kb.create_task(conn, title='exhausted ask', assignee='builder')
        spec = enrollment(conn)
        spec['consumed']['rounds'] = 3
        attempt = state.enroll_review(conn, task, **spec, compatibility=writer_receipts(conn))
        new = kb.create_task(conn, title='authorized successor', assignee='builder')
        decision = {key:attempt[key] for key in ('board_id','spec_digest','base_sha','target_sha')}
        decision.update(operation='successor',attempt_id=attempt['id'],expected_version=0,
                        approved_by='Justin',decision='one finite successor',findings=[],
                        successor={'task_id':new,'base_sha':'b'*40,'target_sha':'c'*40,
                                   'allowance':{'rounds':1,'recovery':0,'active_seconds':60}})
        readiness.begin(conn)
        assert invoke(tmp_path, task, decision) != 0
        assert state.get_attempt(conn, new) is None
        assert state.get_attempt(conn, task)['completed_rounds'] == 3


@pytest.mark.parametrize('module_name,symbol', [
    ('hermes_cli.kanban_review_transport', 'pin_route'), ('hermes_cli.kanban_worker_launch', 'adopt_reserved_launch'),
    ('agent.codex_runtime', 'run_codex_stream'), ('agent.chat_completion_helpers', '_chat_summary_attempt'),
])
def test_changed_transport_or_launch_capability_cannot_claim(tmp_path, monkeypatch, module_name, symbol):
    import importlib
    from hermes_cli import kanban_review_readiness as readiness
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    readiness.runtime_digest.cache_clear()
    try:
        with connect(tmp_path / 'board.db') as conn:
            task = kb.create_task(conn, title='pinned execution enforcement', assignee='builder')
            state.enroll_review(conn, task, **enrollment(conn), compatibility=writer_receipts(conn))
            state.reserve_action(conn, task, category='preflight', expected_version=0)
            with monkeypatch.context() as changed:
                changed.setattr(importlib.import_module(module_name), symbol, lambda *a: None)
                readiness.runtime_digest.cache_clear()
                assert kb.claim_task(conn, task) is None, 'Changed loaded execution enforcement must invalidate writer readiness'
                assert state.get_attempt(conn, task)['state'] == 'held'
    finally:
        readiness.runtime_digest.cache_clear()
