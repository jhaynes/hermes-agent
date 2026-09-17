"""Request-boundary maker admission for enrolled Kanban reviewer workers.

This records installed routing evidence, not remote provider attestation. Unknown
routing and nested delegated reviews are refused rather than guessed.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def before_model_request(agent, request):
    task_id = os.environ.get('HERMES_KANBAN_TASK')
    db_path = os.environ.get('HERMES_KANBAN_DB')
    if not task_id or not db_path:
        return
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_review_state as state
    from hermes_cli.kanban_review_cohort import record_runtime_route
    with connect_closing(Path(db_path)) as conn:
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
        model = request.get('model')
        if ((task['provider_override'] and agent.provider != task['provider_override'])
                or (task['model_override'] and model != task['model_override'])):
            raise PermissionError('effective model route differs from the reserved reviewer route')
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
