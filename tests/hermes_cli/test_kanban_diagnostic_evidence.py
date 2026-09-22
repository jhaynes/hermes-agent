"""Preserved executed receipts support reports, not inferred causal authority."""
import copy
import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_postmortem as reports
from hermes_cli.kanban_db_connect import connect


def test_executed_receipt_report_publishes_but_invented_facts_do_not(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='fixture', assignee='builder')
        run = kb.claim_task(conn, owner).current_run_id
        with kb.write_txn(conn):
            kb._end_run(conn, owner, outcome='timed_out', metadata={'verification_run': [
                {'kind': 'executed', 'command': 'scripts/run_tests.sh tests/fixture.py',
                 'result': 'assert elapsed < limit failed; api_key=synthetic-evidence-secret'}]})
            kb._append_event(conn, owner, 'timed_out', {'elapsed_seconds': 601, 'limit_seconds': 600}, run_id=run)
        kb.block_task(conn, owner, kind='needs_input', reason='Operator decision required')
        reports.queue_reports(conn)
        job = conn.execute('SELECT * FROM workflow_postmortems').fetchone()
        worker = kb.claim_task(conn, job['task_id'])
        context = json.loads(kb.build_worker_context(conn, job['task_id']))
        receipt = next((r for r in context.get('receipts', []) if r.get('citation') == f'run:{run}'), None)
        assert receipt is not None, 'Diagnostic must see bounded preserved test evidence, not just event kinds'
        assert 'synthetic-evidence-secret' not in json.dumps(context)
        report = dict(context['result_contract']['postmortem'])
        report.update(schema=2, citations=[f'run:{run}'], facts=[receipt],
                      confidence='high', confidence_basis='cited_receipt_observation_only',
                      hypotheses=[{'claim': 'The timeout may have interrupted test completion.',
                                   'citations': [f'run:{run}'], 'status': 'unverified'}],
                      contributing_conditions=[{'claim': 'Recorded test failure needs reproduction.',
                                                'citations': [f'run:{run}'], 'status': 'unverified'}],
                      missed_gates=[{'claim': 'It is unknown whether preflight covered this case.',
                                    'citations': [f'run:{run}'], 'status': 'unverified'}],
                      recovery_recommendation={'action': 'Inspect the failed assertion before authorizing another run.',
                                               'execution': 'operator_only', 'citations': [f'run:{run}']},
                      proposed_change={'kind': 'code_change', 'approval_required': True,
                                       'proposal': 'Investigate deadline handling; do not modify the limit automatically.'})
        invented = copy.deepcopy(report)
        invented['facts'][0]['evidence'][0]['result'] = 'The operator caused this.'
        assert not kb.complete_task(conn, job['task_id'], summary='Diagnostic receipt submitted', expected_run_id=worker.current_run_id, metadata={'postmortem': invented})
        assert not kb.list_attachments(conn, owner)
        from hermes_cli.kanban_diagnostic_report import valid
        incident = conn.execute('SELECT * FROM workflow_incidents').fetchone()
        assert valid(conn, incident, report), report
        assert kb.redact_review_value(report) == report
        assert kb.complete_task(conn, job['task_id'], summary='Diagnostic receipt submitted', expected_run_id=worker.current_run_id, metadata={'postmortem': report})
        reports.publish_reports(conn)
        attachments = kb.list_attachments(conn, owner)
        assert len(attachments) == 1
        published = json.loads(Path(attachments[0].stored_path).read_text())
        assert published['hypotheses'][0]['status'] == 'unverified'
        assert published['proposed_change']['approval_required'] is True
        assert 'synthetic-evidence-secret' not in json.dumps(published)
        assert kb.get_task(conn, owner).status == 'blocked'
        assert not conn.execute('SELECT 1 FROM workflow_lessons').fetchone()
