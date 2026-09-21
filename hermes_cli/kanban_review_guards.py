"""Fail-closed managed identity guards shared by legacy SQL writers."""
from __future__ import annotations


def install(conn):
    # An incompatible writer may only park unchanged work in a safe hold.
    # Include every task field so assignment/workspace/budget edits cannot route
    # around the status admission guard. Schema names come from our own table.
    columns = [row[1] for row in conn.execute('PRAGMA table_info(tasks)')
               if row[1] not in {'status','block_kind'}]
    unchanged = ' AND '.join(f'OLD."{name}" IS NEW."{name}"' for name in columns)
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS review_writer_guard
        BEFORE UPDATE ON tasks
        WHEN EXISTS(SELECT 1 FROM review_attempts WHERE task_id=OLD.id)
          OR EXISTS(SELECT 1 FROM review_members WHERE task_id=OLD.id)
        BEGIN SELECT CASE WHEN workflow_writer_compatible(OLD.id)=0
          AND NOT (NEW.status IS 'blocked' AND NEW.block_kind IS 'needs_input' AND {unchanged})
          THEN RAISE(ABORT, 'mixed-version managed workflow writer refused') END; END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS review_frozen_task
        BEFORE UPDATE OF title,body,model_override,provider_override ON tasks
        WHEN (EXISTS(SELECT 1 FROM review_attempts WHERE task_id=OLD.id)
          OR EXISTS(SELECT 1 FROM review_members WHERE task_id=OLD.id))
          AND (OLD.title IS NOT NEW.title OR OLD.body IS NOT NEW.body
            OR OLD.model_override IS NOT NEW.model_override
            OR OLD.provider_override IS NOT NEW.provider_override)
        BEGIN SELECT RAISE(ABORT, 'managed workflow frozen fields require operator amendment'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS review_preserve_task
        BEFORE DELETE ON tasks
        WHEN EXISTS(SELECT 1 FROM review_attempts WHERE task_id=OLD.id)
          OR EXISTS(SELECT 1 FROM review_members WHERE task_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'managed workflow lineage and receipts must be preserved'); END''')


def reject_decomposition(conn, task_id):
    from hermes_cli.kanban_review_state import get_attempt
    if get_attempt(conn, task_id) is not None:
        raise ValueError('managed workflow cannot gain new dispatch authority through decomposition')


def reject_nested_creation(conn, creator_task_id):
    import os
    from hermes_cli.kanban_review_worker import restricted_worker
    from hermes_cli.kanban_postmortem import is_diagnostic
    from hermes_cli.kanban_review_state import get_attempt
    # Implementers must not evade the same lineage by creating an ordinary card.
    # Check the authenticated worker context even when it omits creator metadata.
    creators = {creator_task_id, os.environ.get('HERMES_KANBAN_TASK')} - {None}
    if restricted_worker() or any(get_attempt(conn, task) or is_diagnostic(conn, task)
                                  for task in creators):
        raise ValueError('nested task creation from managed implementation, review or diagnostic workers is prohibited')


def identity_valid(conn, attempt):
    import hashlib
    import json
    from hermes_cli.kanban_review_readiness import compatible
    board = conn.execute('SELECT board_id,schema_version FROM workflow_board WHERE singleton=1').fetchone()
    policy = attempt['policy']
    return (compatible(attempt) and board is not None and tuple(board) == (attempt['board_id'], 1)
            and isinstance(policy, dict) and policy.get('version') == 1
            and hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == attempt['policy_digest'])


def has_live_worker(conn, attempt_id):
    from hermes_cli.kanban_db_dispatch import _worker_alive
    from hermes_cli.kanban_db import _host_prefix
    rows = conn.execute('''SELECT r.worker_pid,r.worker_started_at,r.claim_lock
        FROM task_runs r JOIN review_actions a ON a.run_id=r.id
        WHERE a.attempt_id=? AND r.worker_pid IS NOT NULL''', (attempt_id,)).fetchall()
    for row in rows:
        # Another host cannot prove local quiescence; require its owning reaper.
        if not (row['claim_lock'] or '').startswith(_host_prefix()):
            return True
        if _worker_alive(row['worker_pid'], row['worker_started_at']):
            return True
    return False


def snapshot_matches(conn, attempt):
    """Inspect the owner, not a reviewer-supplied SHA or a prior approval flag."""
    import subprocess
    row = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (attempt['task_id'],)).fetchone()
    if not row or not row[0]:
        return False
    def git(*args):
        return subprocess.check_output(['git', '-C', row[0], *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=10).strip()
    try:
        return (git('rev-parse', 'HEAD') == attempt['target_sha']
                and git('merge-base', attempt['base_sha'], attempt['target_sha']) == attempt['base_sha']
                and not git('status', '--porcelain'))
    except (OSError, subprocess.SubprocessError):
        return False
