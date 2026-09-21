"""Independent processes contend for one spent recovery and one public claim."""
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_review_state as state
from hermes_cli.kanban_db_connect import connect


WORKER = '''import json, sys, time
from pathlib import Path
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_db as kb, kanban_review_state as state
root, db, task, mode, version, gate, output = sys.argv[1:]
assert Path(state.__file__).resolve().is_relative_to(Path(root).resolve())
Path(output + '.ready').touch()
while not Path(gate).exists():
    time.sleep(0.01)
with connect(Path(db)) as conn:
    try:
        if mode == 'reserve':
            result = state.reserve_action(conn,task,category='preflight',expected_version=int(version),recovery=True)
        else:
            row = kb.claim_task(conn,task)
            result = row.current_run_id if row else None
    except ValueError as exc:
        result = {'refused':str(exc)}
Path(output).write_text(json.dumps(result))
'''


def test_recovery_and_claim_are_atomic_across_processes(tmp_path, monkeypatch):
    import time
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    path = tmp_path / 'board.db'
    conn = connect(path)
    task = kb.create_task(conn,title='shared recovery',assignee='builder')
    attempt = state.enroll_review(conn,task,expected_status='ready',expected_run_id=None,expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],spec_digest='a'*64,
        base_sha='b'*40,target_sha='c'*40,implementer_maker='openai',roster=sorted(state.REQUIRED_LANES),
        consumed={'rounds':0,'recovery':1,'active_seconds':0},compatibility=writer_receipts(conn),decision='synthetic')
    state.reserve_action(conn,task,category='preflight',expected_version=0)
    assert kb.claim_task(conn,task)
    dispatch._record_task_failure(conn,task,'synthetic failure',outcome='spawn_failed',failure_limit=100,release_claim=True,end_run=True)
    version = state.get_attempt(conn,task)['version']
    worker = tmp_path / 'contend.py'
    worker.write_text(WORKER)
    root = Path(__file__).resolve().parents[2]
    processes = []
    try:
        for mode in ('reserve','claim'):
            gate = tmp_path / (mode + '.go')
            outputs = [tmp_path / f'{mode}-{i}.json' for i in range(2)]
            for output in outputs:
                processes.append(subprocess.Popen([sys.executable,str(worker),str(root),str(path),task,mode,str(version),str(gate),str(output)],env=dict(os.environ)))
            until = time.monotonic() + 10
            while not all(Path(str(p)+'.ready').exists() for p in outputs) and time.monotonic() < until:
                time.sleep(0.02)
            assert all(Path(str(p)+'.ready').exists() for p in outputs)
            gate.touch()
            for process in processes:
                assert process.wait(timeout=15) == 0
            results = [json.loads(p.read_text()) for p in outputs]
            assert sum(isinstance(r,(str,int)) for r in results) == 1
            assert state.get_attempt(conn,task)['recovery_used'] == 2
        run = kb.get_task(conn,task).current_run_id
        assert run is not None
        assert conn.execute("SELECT COUNT(*) FROM review_actions WHERE state='running'").fetchone()[0] == 1
        dispatch._record_task_failure(conn,task,'second synthetic failure',outcome='spawn_failed',failure_limit=100,release_claim=True,end_run=True)
        current = state.get_attempt(conn,task)
        assert state.reserve_action(conn,task,category='preflight',expected_version=current['version'],recovery=True) is None
        assert state.get_attempt(conn,task)['state'] == 'held'
        assert kb.claim_task(conn,task) is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=8)
        conn.close()
