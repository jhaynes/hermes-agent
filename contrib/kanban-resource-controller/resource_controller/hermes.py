"""Read-only canonical SQLite contract for the pinned Hermes CLI baseline."""
import json
import sqlite3
import time

BASE = '5910de20bc9839fdd36e791a9d72ba2c2e722f66'
COLUMNS = {
    'tasks': {'id', 'assignee', 'status', 'current_run_id', 'claim_lock', 'claim_expires', 'priority', 'created_at'},
    'task_runs': {'id', 'task_id', 'profile', 'status', 'worker_pid', 'claim_expires'},
    'task_events': {'id', 'task_id', 'run_id', 'kind', 'payload'},
    'kanban_notify_subs': {'notifier_profile'},
}


def config_holds(cfg):
    required = {'dispatch_in_gateway': False, 'max_in_progress': 2,
                'max_in_progress_per_profile': 1, 'dispatch_stale_timeout_seconds': 0,
                'reconcile_orphans': True}
    holds = [f'compatibility:config:{key}' for key, value in required.items()
             if type(cfg.get(key)) is not type(value) or cfg[key] != value]
    if type(cfg.get('auto_decompose')) is not bool:
        holds.append('compatibility:config:auto_decompose')
    if type(cfg.get('failure_limit')) is not int or cfg['failure_limit'] < 1:
        holds.append('compatibility:config:failure_limit')
    return holds


def read_board(path, slug):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + 3
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        for table, expected in COLUMNS.items():
            actual = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            if not expected <= actual:
                raise ValueError(f'compatibility:schema:{slug}:{table}')
        def rows(sql):
            result = [dict(r) for r in conn.execute(sql).fetchmany(10001)]
            if len(result) > 10000:
                raise ValueError(f'compatibility:inventory-too-large:{slug}')
            return result
        tasks = rows('SELECT id,assignee,status,current_run_id,claim_lock,claim_expires,priority,created_at FROM tasks')
        runs = rows('SELECT id AS run_id,task_id,profile,status,worker_pid AS pid,claim_expires FROM task_runs')
        events = rows('SELECT task_id,run_id,payload FROM task_events WHERE kind="spawned" ORDER BY id')
        stamps = {}
        for event in events:
            payload = json.loads(event['payload'])
            stamps[(event['task_id'], event['run_id'])] = payload
        holds = []
        by_task = {task['id']: task for task in tasks}
        for run in runs:
            event = stamps.get((run['task_id'], run['run_id']), {})
            # _end_run deliberately clears PID on both task and run. The
            # canonical spawned event retains the exact original fingerprint.
            if run['status'] != 'running' and run['pid'] is None:
                run['pid'] = event.get('pid')
            run.update(board=slug, stamp=event.get('started_at') if event.get('pid') == run['pid'] else None)
            task = by_task.get(run['task_id'])
            if run['status'] == 'running' and (task is None or task['status'] != 'running'
                    or task['current_run_id'] != run['run_id'] or task['assignee'] != run['profile']):
                holds.append(f"identity:run-task-mismatch:{slug}:{run['run_id']}")
        running_ids = {r['run_id'] for r in runs if r['status'] == 'running'}
        for task in tasks:
            if task['status'] == 'running' and (task['current_run_id'] not in running_ids
                    or not task['claim_lock'] or not task['claim_expires']):
                holds.append(f"identity:orphan-claim:{slug}:{task['id']}")
        if conn.execute("SELECT 1 FROM kanban_notify_subs WHERE notifier_profile IS NULL OR trim(notifier_profile)='' LIMIT 1").fetchone():
            holds.append(f'compatibility:unowned-notifications:{slug}')
        return {'runs': runs, 'tasks': tasks, 'holds': holds}
    finally:
        conn.close()
