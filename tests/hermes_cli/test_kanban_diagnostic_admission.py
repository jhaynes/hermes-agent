"""Legacy adjudication cannot reclassify diagnostics as implementation work."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_postmortem as reports
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from tests.hermes_cli.test_kanban_review_operator import invoke


@pytest.mark.parametrize('disposition', ['preserve_legacy', 'enroll', 'successor'])
def test_diagnostic_cannot_use_legacy_adjudication(tmp_path, monkeypatch, capsys, disposition):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    with connect(db) as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        assert invoke(tmp_path, task, {'operation': 'legacy-history', 'disposition': 'inspect'}) == 0
        observed = json.loads(capsys.readouterr().out)
        receipt = {'operation': 'legacy-history', 'disposition': disposition,
                   'history_digest': observed['history_digest'], 'board_id': observed['board_id'],
                   'expected_status': observed['status'], 'expected_run_id': None,
                   'spec_digest': observed['spec_digest'], 'base_sha': 'b'*40, 'target_sha': 'c'*40,
                   'approved_by': 'Justin', 'decision': 'synthetic prohibited reclassification',
                   'consumed': {'rounds': 0, 'recovery': 0, 'active_seconds': 0},
                   'compatibility': writer_receipts(conn), 'implementer_maker': 'openai',
                   'roster': sorted(state.REQUIRED_LANES)}
        if disposition == 'successor':
            successor = kb.create_task(conn, title='replacement', assignee='builder')
            receipt['successor'] = {'task_id': successor, 'base_sha': 'b'*40, 'target_sha': 'c'*40,
                                    'allowance': {'rounds': 1, 'recovery': 0, 'active_seconds': 60},
                                    'remaining_finding_events': []}
        assert invoke(tmp_path, task, receipt) != 0
        assert not conn.execute('SELECT 1 FROM review_legacy_adjudications WHERE task_id=?', (task,)).fetchone()
        assert not conn.execute('SELECT 1 FROM review_attempts').fetchone()
