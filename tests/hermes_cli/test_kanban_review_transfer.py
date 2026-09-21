"""A writable imported board is not authority to resume copied review work."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_review_state as state
from hermes_cli import kanban_transfer as transfer
from hermes_cli.kanban_db_connect import connect_closing


def test_managed_export_import_preserves_history_but_not_launch_authority(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'home'))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD'):
        monkeypatch.delenv(key, raising=False)
    kb.create_board('source')
    with connect_closing(board='source') as conn:
        owner = kb.create_task(conn, title='finite imported work', assignee='builder')
        attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None, expected_assignee='builder',
            board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
            spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40,
            implementer_maker='openai', roster=sorted(state.REQUIRED_LANES),
            consumed={'rounds': 1, 'recovery': 1, 'active_seconds': 100},
            compatibility=writer_receipts(conn), decision='synthetic')
        state.reserve_action(conn, owner, category='preflight', expected_version=0)
        assert kb.claim_task(conn, owner) is not None
    try:
        exported = transfer.export_board('source', str(tmp_path / 'snapshot'))
    except sqlite3.DatabaseError as exc:
        pytest.fail(f'Supported export must understand the managed downgrade guard: {exc}')
    imported = transfer.import_board(exported['archive'], slug='copy')
    with connect_closing(board='copy') as clone:
        copy = state.get_attempt(clone, owner)
        assert clone.execute('SELECT board_id FROM workflow_board').fetchone()[0] != attempt['board_id']
        assert copy['id'] == attempt['id'], 'Retain provenance, never mint a fresh budget on import'
        assert copy['state'] == 'held'
        assert copy['completed_rounds'] == 1 and copy['recovery_used'] == 1
        assert copy['active_seconds'] >= 100
        assert kb.get_task(clone, owner).current_run_id is None
        assert kb.claim_task(clone, owner) is None
        assert not kb.complete_task(clone, owner, force=True)
        assert not clone.execute("SELECT 1 FROM review_actions WHERE state IN ('running','reserved')").fetchone()
    with connect_closing(board='source') as original:
        assert state.get_attempt(original, owner)['state'] == 'preflight'
        assert kb.get_task(original, owner).current_run_id is not None
