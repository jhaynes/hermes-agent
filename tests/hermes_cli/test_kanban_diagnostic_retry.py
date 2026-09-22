"""Only observed infrastructure failures authorize another synthesis."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect


@pytest.mark.parametrize('failure', ['content_rejected', 'protocol_violation', 'unknown_exit'])
def test_non_infrastructure_failure_cannot_mint_retry(tmp_path, monkeypatch, failure):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: p == 'diagnostic')
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='failure', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t), max_spawn=1)
        reporter = spawned[0]
        if failure == 'content_rejected':
            report = json.loads(kb.build_worker_context(conn, reporter.id))['result_contract']['postmortem']
            report['facts'] = ['unsupported personal blame']
            assert not kb.complete_task(conn, reporter.id, summary='Diagnostic receipt submitted', expected_run_id=reporter.current_run_id,
                                        metadata={'postmortem': report})
        metadata = {'exit_kind': 'signaled', 'exit_code': 9} if failure == 'content_rejected' else (
            {'protocol_violation': True, 'exit_code': 0} if failure == 'protocol_violation' else {})
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (reporter.id,))
            kb._end_run(conn, reporter.id, outcome='crashed', metadata=metadata)
        dispatch.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t), max_spawn=1)
        assert len(spawned) == 1, 'Content/protocol/unknown failures are not infrastructure retry authority'
        assert conn.execute('SELECT report_status FROM workflow_incidents').fetchone()[0] == 'synthesis_failed'
        assert kb.get_task(conn, owner).status == 'blocked'
