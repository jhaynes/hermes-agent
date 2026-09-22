"""Exact argv + canonical run identity reconciliation, including completed cards."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import re

TASK_QUERY = re.compile(r'work kanban task (t_[0-9a-f]{8})\Z')


@dataclass(frozen=True)
class Process:
    pid: int
    created: float
    parent: int
    argv: list[str]


class WorkerParser:
    def __init__(self, prefix, parser, inherited):
        self.prefixes = [prefix] if isinstance(prefix[0], str) else prefix
        self.parser = parser
        self.profile_flags = {name for name, takes_value in inherited if takes_value}
        self.parser.allow_abbrev = False
        self.parser.add_argument(*sorted(self.profile_flags), dest='worker_profile')

    def parse(self, argv):
        prefix = next((p for p in self.prefixes if argv[:len(p)] == p), None)
        if prefix is None:
            return None
        # Use the actual parser, so prompt-looking flag VALUES never become queries.
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            try:
                args = self.parser.parse_args(argv[len(prefix):])
            except (SystemExit, ValueError):
                return None
        query = getattr(args, 'query', None)
        match = TASK_QUERY.fullmatch(query or '')
        if getattr(args, 'command', None) != 'chat' or not match or not args.worker_profile:
            return None
        return match[1], args.worker_profile


def descendants(pid, processes):
    found = set()
    frontier = {pid}
    while frontier:
        frontier = {p.pid for p in processes if p.parent in frontier and p.pid not in found and p.pid != pid}
        found.update(frontier)
    return sorted(found)


def reconcile(rows, processes, parser, now):
    by_pid = {p.pid: p for p in processes}
    holds, workers, matched = [], [], set()
    seen = set()
    for row in rows:
        process = by_pid.get(row['pid'])
        key = (row['board'], row['run_id'])
        if key in seen:
            holds.append('identity:duplicate-run')
            continue
        seen.add(key)
        if process is None:
            if row['status'] == 'running':
                holds.append(f"identity:stale-claim:{row['board']}:{row['task_id']}")
            continue
        identity = parser.parse(process.argv)
        if (row['stamp'] is None or round(process.created * 100) != row['stamp']
                or identity != (row['task_id'], row['profile'])):
            # Historical PIDs get reused normally; an expired historical process is
            # not ours. A live worker or active claim with conflicting identity IS a hold.
            if (identity is not None or row['status'] == 'running' or row['stamp'] is None
                    or round(process.created * 100) == row['stamp']):
                holds.append(f"identity:conflict:{row['board']}:{row['task_id']}")
            continue
        process_key = (process.pid, process.created)
        if process_key in matched:
            holds.append('identity:duplicate-process')
            continue
        matched.add(process_key)
        if row['status'] == 'running' and (not row['claim_expires'] or row['claim_expires'] <= now):
            holds.append(f"identity:expired-claim:{row['task_id']}")
        workers.append({'board': row['board'], 'task_id': row['task_id'], 'run_id': row['run_id'],
                        'profile': row['profile'], 'pid': process.pid, 'created': process.created,
                        'parent': process.parent, 'descendants': descendants(process.pid, processes)})
    for process in processes:
        if (process.pid, process.created) in matched:
            continue
        if parser.parse(process.argv) is not None or any(TASK_QUERY.fullmatch(a) for a in process.argv):
            holds.append(f'identity:unowned-worker:{process.pid}')
    return {'workers': workers, 'holds': sorted(set(holds))}
