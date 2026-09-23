"""Pinned operator review runner; no board mutation or live enrollment."""

import json
import os
from pathlib import Path
import signal
import subprocess
import time


def run_lane(home, lane, run_id):
    import fcntl
    home = Path(home)
    manifest = json.loads((home / f'{lane}.json').read_text())
    tree = Path(manifest['tree'])
    assert manifest['task_id']
    journal = home / f'{lane}-receipts.jsonl'

    def git(*args):
        return subprocess.check_output(['git', '-C', str(tree), *args], text=True).strip()

    def identity():
        assert git('rev-parse', '--show-toplevel') == str(tree)
        assert git('rev-parse', 'HEAD') == manifest['sha']
        assert git('merge-base', manifest['base'], manifest['sha']) == manifest['base']
        assert not git('status', '--porcelain')
        return {'cwd': str(tree), 'head': manifest['sha'], 'base': manifest['base'],
                'git_dir': git('rev-parse', '--git-dir'), 'common_dir': git('rev-parse', '--git-common-dir')}

    env = dict(os.environ)
    env['PYTHONPATH'] = str(tree / '.hermes/review-harness') + os.pathsep + str(tree)
    env['HERMES_TEST_FILE_RETRIES'] = '0'
    proof_command = manifest['commands'][0]
    assert len(manifest['commands']) >= 2 and manifest['commands'][-1] == proof_command
    def step(index, command, receipts):
        log = home / f'{lane}-{index}.log'
        record = {'lane': lane, 'task_id': manifest['task_id'], 'run_id': run_id,
                  'identity': {}, 'command': command, 'import_proof': '',
                  'started': time.time(), 'exit_code': 125, 'log': str(log), 'clean': False}
        with log.open('x') as output:
            try:
                record['identity'] = identity()
                remaining = manifest['deadline'] - time.time()
                assert remaining > 0, 'Allowance deadline reached'
                proof = subprocess.run(proof_command, cwd=tree, env=env, text=True,
                                       capture_output=True, timeout=min(30, remaining))
                record['import_proof'] = proof.stdout.strip()
                assert proof.returncode == 0, proof.stdout + proof.stderr
                remaining = manifest['deadline'] - time.time()
                # Reserve finalization time rather than spending the entire
                # allowance on a test that can never produce a final receipt.
                timeout = min(1200, remaining - (30 if command[0] == 'nice' else 0))
                assert timeout > 0, 'Insufficient time before final identity'
                child = subprocess.Popen(command, cwd=tree, env=env, stdout=output,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    record['exit_code'] = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    record['exit_code'] = 124
                record['clean'] = not git('status', '--porcelain')
            except (AssertionError, OSError, subprocess.SubprocessError) as error:
                record['error'] = str(error)
                output.write(str(error) + '\n')
        record['finished'] = time.time()
        receipts.write(json.dumps(record) + '\n')
        receipts.flush()
        print(json.dumps(record), flush=True)
        print(log.read_text(), flush=True)
        return record['exit_code']

    exit_code = 0
    final_index = len(manifest['commands']) - 1
    with journal.open('x') as receipts:
        try:
            for index, command in enumerate(manifest['commands'][:-1]):
                with (home / 'test.lock').open('a') as lock:
                    if command[0] == 'nice':
                        print('Waiting for the shared serial test slot', flush=True)
                        while time.time() < manifest['deadline'] - 30:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                time.sleep(1)
                        else:
                            raise TimeoutError('Test slot unavailable before finalization reserve')
                    code = step(index, command, receipts)
                    exit_code = exit_code or code
        finally:
            final_code = step(final_index, manifest['commands'][-1], receipts)
            exit_code = exit_code or final_code
    print('Sequence finished. final_receipt=' + str(home / f'{lane}-{final_index}.log'))
    return exit_code
