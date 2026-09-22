"""Serial standalone unittest runner; optional durable RED/GREEN receipts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--receipt-dir', type=Path, required=True)
parser.add_argument('--label', required=True)
parser.add_argument('--pattern', default='test_*.py')
args = parser.parse_args()
root = Path(__file__).resolve().parent
args.receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
with tempfile.TemporaryDirectory(prefix='controller-test-') as home:
    env = {'HOME': home, 'HERMES_HOME': home, 'HERMES_KANBAN_HOME': home,
           'PATH': os.environ['PATH'], 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1',
           'TZ': 'UTC', 'LANG': 'C.UTF-8', 'PYTHONHASHSEED': '0',
           'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
           'VECLIB_MAXIMUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'}
    cmd = [sys.executable, '-m', 'unittest', 'discover', '-s', str(root / 'tests'), '-p', args.pattern, '-v']
    start = time.time()
    result = subprocess.run(cmd, cwd=root, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=180)
    receipt = {'label': args.label, 'command': cmd, 'exit_code': result.returncode,
               'started_at': start, 'finished_at': time.time(), 'output': result.stdout,
               'isolation': 'temporary HOME/HERMES_HOME/KANBAN_HOME, no inherited credentials',
               'niceness': os.getpriority(os.PRIO_PROCESS, 0), 'native_threads': 1}
    path = args.receipt_dir / (args.label + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(receipt, stream, indent=2)
    print(result.stdout, end='')
    print('Receipt:', path)
    sys.exit(result.returncode)
