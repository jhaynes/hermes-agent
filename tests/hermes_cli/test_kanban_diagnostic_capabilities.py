"""Diagnostics expose only bounded board evidence and their own result channel."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_postmortem as reports
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_review_worker import construction_tools


def test_diagnostic_construction_and_context_exclude_ambient_authority(tmp_path, monkeypatch, caplog):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('kanban:\n  review_feedback:\n    postmortem_profile: diagnostic\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = tmp_path / 'board.db'
    with connect(db) as conn:
        owner = kb.create_task(conn, title='private source', assignee=None)
        kb.block_task(conn, owner, kind='capability', reason='api_key=synthetic-secret')
        reports.queue_reports(conn)
        task = conn.execute('SELECT task_id FROM workflow_postmortems').fetchone()[0]
        monkeypatch.setenv('HERMES_KANBAN_TASK', task)
        monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
        names = ['kanban_show', 'kanban_complete', 'kanban_heartbeat', 'terminal',
                 'execute_code', 'memory', 'write_file', 'skill_manage', 'kanban_block',
                 'kanban_comment', 'kanban_attach', 'delegate_task', 'cronjob']
        tools = [{'function': {'name': name}} for name in names]
        actual = {tool['function']['name'] for tool in construction_tools(tools)}
        assert actual == {'kanban_show', 'kanban_complete', 'kanban_heartbeat'}
        kb.add_comment(conn, task, author='external', body='api_key=synthetic-secret')
        context = kb.build_worker_context(conn, task)
        assert 'synthetic-secret' not in context
        assert json.loads(context)['incident_id']
        from tools.kanban_tools import _handle_show
        shown = json.loads(_handle_show({'task_id': task}))
        assert 'synthetic-secret' not in json.dumps(shown)
        assert 'error' in json.loads(_handle_show({'task_id': owner}))
        from tools.kanban_tools import _handle_block, _handle_complete
        assert 'error' in json.loads(_handle_block({'reason': 'change lifecycle', 'kind': 'transient'}))
        assert 'error' in json.loads(_handle_complete({'summary': 'artifact leak', 'artifacts': [str(db)]}))
        assert kb.get_task(conn, task).status == 'ready'
        from types import SimpleNamespace
        from tools import kanban_tools
        monkeypatch.setattr(kanban_tools, '_comment_poll_last_attempt', 0)
        monkeypatch.setitem(kanban_tools._comment_watermark, task, 0)
        steered = []
        kanban_tools.inject_new_comments_from_env(SimpleNamespace(steer=lambda text: steered.append(text)))
        assert not steered, 'Raw comments cannot bypass the bounded diagnostic evidence channel'
        def unavailable(*args, **kwargs):
            raise RuntimeError('synthetic-redactor-secret')
        monkeypatch.setattr(kanban_tools, 'redact_sensitive_text', unavailable)
        reply = _handle_complete({'summary': 'fixture'})
        assert 'error' in json.loads(reply)
        assert 'synthetic-redactor-secret' not in reply + caplog.text
