"""Existing stale threshold diagnoses missing owners, not capacity waits."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect


def test_stale_unowned_queue_is_diagnosed_once_without_changing_ownership(tmp_path):
    with connect(tmp_path / 'board.db') as conn:
        task = kb.create_task(conn, title='unowned', assignee=None)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET created_at=1 WHERE id=?', (task,))
            conn.execute('UPDATE task_events SET created_at=1 WHERE task_id=?', (task,))
        dispatch.dispatch_once(conn, max_spawn=0, stale_timeout_seconds=10)
        rows = conn.execute('SELECT * FROM workflow_incidents WHERE task_id=?', (task,)).fetchall()
        assert len(rows) == 1
        assert rows[0]['classification'] == 'stalled_queue'
        assert kb.get_task(conn, task).status == 'ready'
        dispatch.dispatch_once(conn, max_spawn=0, stale_timeout_seconds=10)
        assert conn.execute('SELECT COUNT(*) FROM workflow_incidents WHERE task_id=?', (task,)).fetchone()[0] == 1


def test_existing_priority_queue_cannot_guarantee_finite_diagnostic_service(tmp_path, monkeypatch):
    """Rollout hold receipt, not a claim that starvation is solved."""
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: True)
    with connect(tmp_path / 'board.db') as conn:
        owner = kb.create_task(conn, title='failed owner', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='fixture')
        for index in range(3):
            builder = kb.create_task(conn, title=f'queued builder {index}', assignee='builder', priority=100)
            spawned = []
            dispatch.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t), max_spawn=1,
                                   max_in_progress_per_profile=1)
            assert [task.id for task in spawned] == [builder]
            assert kb.complete_task(conn, builder, expected_run_id=spawned[0].current_run_id)
        diagnostic = conn.execute('SELECT task_id,runs_started FROM workflow_postmortems').fetchone()
        assert diagnostic['runs_started'] == 0
        assert kb.get_task(conn, diagnostic['task_id']).status == 'ready'
        # No extra profile/board capacity or invented failure was used to skip the queue.
        assert conn.execute('SELECT COUNT(*) FROM workflow_incidents').fetchone()[0] == 1
