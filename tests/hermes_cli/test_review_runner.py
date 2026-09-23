"""Failed probes remain evidence and never suppress final identity."""
import json
import subprocess
import sys
import time

import pytest

from tests.hermes_cli.review_runner import run_lane


@pytest.mark.macos_only
@pytest.mark.parametrize('exit_code', [0, 7, 125])
def test_runner_preserves_final_identity_and_failed_exit(tmp_path, exit_code):
    tree = tmp_path / 'tree'
    tree.mkdir()
    subprocess.run(['git', 'init', str(tree)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(tree), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'fixture'],
                   check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
    proof = [sys.executable, '-c', 'print("identity")']
    probe = ([str(tree / 'missing-executable')] if exit_code == 125 else
             [sys.executable, '-c', f'raise SystemExit({exit_code})'])
    commands = [proof, probe, proof]
    manifest = {'tree': str(tree), 'task_id': 'fixture', 'sha': sha, 'base': sha,
                'deadline': time.time() + 60, 'commands': commands}
    (tmp_path / 'tests.json').write_text(json.dumps(manifest))
    assert run_lane(tmp_path, 'tests', 1) == exit_code
    receipts = [json.loads(line) for line in (tmp_path / 'tests-receipts.jsonl').read_text().splitlines()]
    assert [r['command'] for r in receipts] == commands
    assert [r['exit_code'] for r in receipts] == [0, exit_code, 0]
    assert receipts[-1]['clean'] is True
    assert receipts[-1]['identity']['head'] == sha
