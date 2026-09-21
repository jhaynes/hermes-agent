"""Adopt a durable managed launch when its dispatcher lost the PID receipt."""
import os
import threading


_children = {}
_children_lock = threading.Lock()


def retain_child(process):
    if os.name == 'nt':
        return
    # Popen.__del__ otherwise registers a live child for subprocess._cleanup,
    # which silently consumes its exit status on an unrelated process launch.
    with _children_lock:
        _children[process.pid] = process


def reap_children(record_exit):
    reaped = []
    with _children_lock:
        for pid, process in list(_children.items()):
            code = process.poll()
            if code is None:
                continue
            # The existing registry stores POSIX wait status, not returncode.
            record_exit(pid, code << 8 if code >= 0 else -code)
            del _children[pid]
            reaped.append(pid)
    return reaped


def adopt_reserved_launch(conn, task_id, run_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    with write_txn(conn, allow_nested=True):
        run = conn.execute('''SELECT r.worker_pid,r.claim_lock,r.ended_at,t.current_run_id,t.status
            FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND t.id=?''',
                           (run_id, task_id)).fetchone()
        if (not run or run['current_run_id'] != run_id or run['status'] != 'running'
                or run['ended_at'] is not None):
            raise PermissionError('managed launch reservation is no longer current')
        if run['worker_pid'] is not None:
            return
        if not str(run['claim_lock'] or '').startswith(kb._host_prefix()):
            raise PermissionError('managed launch reservation belongs to another host')
        pid = os.getpid()
        fingerprint = _process_fingerprint(pid)
        if not fingerprint:
            raise PermissionError('managed process identity unavailable')
        conn.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?',
                     (pid, fingerprint, run_id))
        conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=? AND current_run_id=?',
                     (pid, fingerprint, task_id, run_id))
        kb._append_event(conn, task_id, 'spawned', {'pid': pid, 'started_at': fingerprint}, run_id=run_id)
