"""Two independent connections cannot claim or publish duplicate diagnostic work."""
import json
from concurrent.futures import ThreadPoolExecutor

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_postmortem as reports
from hermes_cli.kanban_db_connect import connect, connect_closing


def test_concurrent_claim_completion_and_outbox_are_single_use(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    db = tmp_path / 'board.db'
    with connect(db) as conn:
        owner = kb.create_task(conn, title='fixture', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='failure')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]

    def claim(_):
        with connect_closing(db) as conn:
            return kb.claim_task(conn, task)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = [run for run in pool.map(claim, range(2)) if run is not None]
    assert len(claimed) == 1
    with connect(db) as conn:
        report = json.loads(kb.build_worker_context(conn, task))['result_contract']['postmortem']

    def complete(_):
        with connect_closing(db) as conn:
            return kb.complete_task(conn, task, summary='Diagnostic receipt submitted', expected_run_id=claimed[0].current_run_id, metadata={'postmortem': report})

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(complete, range(2))).count(True) == 1

    def publish(_):
        with connect_closing(db) as conn:
            reports.publish_reports(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(publish, range(2)))
    with connect(db) as conn:
        assert len(kb.list_attachments(conn, owner)) == 1
        assert conn.execute('SELECT runs_started FROM workflow_postmortems').fetchone()[0] == 1
        assert kb.get_task(conn, owner).status == 'blocked'
