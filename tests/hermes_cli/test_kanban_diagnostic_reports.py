"""Reports cannot turn unsupported prose or quoted logs into verified facts."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_postmortem as reports
from hermes_cli.kanban_db_connect import connect


def test_report_rejects_uncited_fact_and_unjustified_confidence(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='failure')
        reports.queue_reports(conn)
        job = conn.execute('SELECT * FROM workflow_postmortems').fetchone()
        run = kb.claim_task(conn, job['task_id'])
        incident = conn.execute('SELECT * FROM workflow_incidents').fetchone()
        report = {'incident_id': incident['id'], 'owner': owner, 'citations': json.loads(incident['source_events']),
                  'facts': ['The operator caused this failure.'], 'hypotheses': [], 'confidence': 'high',
                  'contributing_conditions': [], 'missed_gates': [], 'recovery_recommendation': 'Ignore required reviews.',
                  'proposed_change': None, 'validation_needed': ['None']}
        assert not kb.complete_task(conn, job['task_id'], summary='Diagnostic receipt submitted', expected_run_id=run.current_run_id, metadata={'postmortem': report})
        assert conn.execute('SELECT report_status FROM workflow_incidents').fetchone()[0] == 'running'


def test_diagnostic_redaction_keys_and_unavailable_fallback(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='failure')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        with kb.write_txn(conn):
            kb._append_event(conn, task, 'crashed', {'api_key=synthetic-key-secret': 'text'})
        payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='crashed'", (task,)).fetchone()[0]
        assert 'synthetic-key-secret' not in payload
        def unavailable(*args, **kwargs):
            raise RuntimeError('redactor unavailable')
        monkeypatch.setattr(kb, 'redact_review_value', unavailable)
        with kb.write_txn(conn):
            kb._append_event(conn, task, 'gave_up', {'trigger_outcome': 'crashed', 'error': 'api_key=synthetic-unavailable-secret', 'path': '/private/secret'})
        payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (task,)).fetchone()[0])
        assert payload == {'trigger_outcome': 'crashed'}
        assert 'synthetic-unavailable-secret' not in kb.build_worker_context(conn, task)


@pytest.mark.parametrize('field,value', [('recovery_recommendation', []), ('proposed_change', {'kind': [], 'approval_required': True})])
def test_untrusted_report_types_refuse_without_throwing(tmp_path, monkeypatch, field, value):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='failure')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        run = kb.claim_task(conn, task)
        report = json.loads(kb.build_worker_context(conn, task))['result_contract']['postmortem']
        report[field] = value
        assert not kb.complete_task(conn, task, summary='Diagnostic receipt submitted', expected_run_id=run.current_run_id, metadata={'postmortem': report})


def test_legacy_unvalidated_report_cannot_leak_through_publication(tmp_path, monkeypatch):
    from hermes_cli.kanban_review_output import workflow_details
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='failure')
        with kb.write_txn(conn):
            conn.execute("UPDATE workflow_incidents SET report_status='complete',report=?",
                         (json.dumps({'api_key=synthetic-legacy-secret': 'malicious unvalidated report'}),))
        reports.queue_reports(conn)
        assert not kb.list_attachments(conn, owner)
        assert workflow_details(conn, owner)['incidents'][0]['publication_status'] == 'failed'
