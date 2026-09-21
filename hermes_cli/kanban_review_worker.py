"""Request-boundary maker admission for enrolled Kanban reviewer workers.

This records installed routing evidence, not remote provider attestation. Unknown
routing and nested delegated reviews are refused rather than guessed.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def restricted_worker():
    """Reviewer/diagnostic identity is board-owned, not a requested toolset."""
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    db_path = os.environ.get('HERMES_KANBAN_DB')
    if not task_id or not db_path:
        return False
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_postmortem import is_diagnostic
    with connect_closing(Path(db_path)) as conn:
        return bool(conn.execute('SELECT 1 FROM review_members WHERE task_id=?', (task_id,)).fetchone()
                    or is_diagnostic(conn, task_id))


def construction_tools(tools):
    # Initial snapshot only. This is not a filesystem/network sandbox.
    from hermes_cli.kanban_diagnostic_worker import current_task, TOOLS
    if current_task():
        return [tool for tool in tools if tool['function']['name'] in TOOLS]
    if not restricted_worker():
        return tools
    return [tool for tool in tools if tool['function']['name'] not in
            {'delegate_task', 'kanban_create', 'cronjob'}]


def before_model_request(agent, request):
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    db_path = os.environ.get('HERMES_KANBAN_DB')
    if not task_id or not db_path:
        return
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_review_cohort import record_runtime_route
    with connect_closing(Path(db_path)) as conn:
        from hermes_cli.kanban_diagnostic_worker import before_request
        if before_request(conn, task_id, agent):
            from hermes_cli.kanban_review_transport import pin_route
            pin_route(agent)
            return
        member = conn.execute('SELECT * FROM review_members WHERE task_id=?', (task_id,)).fetchone()
        attempt = state.get_attempt(conn, task_id)
        if attempt is None:
            return
        from hermes_cli.kanban_review_guards import identity_valid
        if not identity_valid(conn, attempt):
            raise PermissionError('managed workflow identity or schema changed before request')
        task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        run_id = os.environ.get('HERMES_KANBAN_RUN_ID', '')
        if not run_id.isdecimal() or task['current_run_id'] != int(run_id):
            raise InterruptedError('managed review run is no longer current')
        phases = {'reviewing'} if member else {'preflight', 'repair'}
        if attempt['state'] not in phases or getattr(agent, 'is_subagent', False):
            raise PermissionError('managed review is not admitted; nested review is prohibited')
        if getattr(agent, '_fallback_chain', None):
            raise PermissionError('enrolled review requires a pinned route without unverified fallback')
        if not isinstance(agent.max_iterations, int) or agent.max_iterations <= 0:
            raise PermissionError('enrolled review requires a finite iteration budget')
        action = conn.execute('SELECT deadline FROM review_actions WHERE task_id=? AND run_id=? AND state=\'running\'', (task_id,int(run_id))).fetchone()
        if not action or time.time() >= action['deadline']:
            raise TimeoutError('managed review execution deadline reached')
        from hermes_cli.kanban_worker_launch import adopt_reserved_launch
        adopt_reserved_launch(conn, task_id, int(run_id))
        model = request.get('model')
        if ((task['provider_override'] and agent.provider != task['provider_override'])
                or (task['model_override'] and model != task['model_override'])):
            raise PermissionError('effective model route differs from the reserved reviewer route')
        from hermes_cli.kanban_review_transport import pin_route
        pin_route(agent)
        if not member:
            record_runtime_route(conn, task_id, int(run_id), provider=agent.provider, model=model, isolated=False)
            return
        workspace = Path(task['workspace_path']).resolve()
        owner_path = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (attempt['task_id'],)).fetchone()[0]
        def git(*args):
            result = subprocess.run(['git','-C',str(workspace),*args],capture_output=True,text=True,timeout=10,check=True)
            return result.stdout.strip()
        isolated = (workspace == Path.cwd().resolve() and owner_path and workspace != Path(owner_path).resolve()
                    and git('rev-parse','HEAD') == attempt['target_sha']
                    and git('merge-base',attempt['base_sha'],attempt['target_sha']) == attempt['base_sha']
                    and not git('status','--porcelain')
                    and git('rev-parse','--absolute-git-dir') != str(Path(git('rev-parse','--git-common-dir')).resolve()))
        record_runtime_route(conn,task_id,int(run_id),provider=agent.provider,model=model,isolated=bool(isolated))
