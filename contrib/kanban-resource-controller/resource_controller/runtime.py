"""Pinned CLI adapter. All production Kanban mutations use supported CLI verbs."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import psutil

from .command import Command
from .hermes import BASE, config_holds, read_board
from .inventory import Process, WorkerParser, reconcile
from .lifecycle import DrainTracker
from .storage import Store

SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')


@dataclass(frozen=True)
class Settings:
    source: Path
    python: Path
    home: Path
    user_home: Path
    state: Path

    def __post_init__(self):
        for path in (self.source, self.python, self.home, self.user_home, self.state):
            if not path.is_absolute():
                raise ValueError('all controller paths must be absolute')


def verify_source(source):
    # The only allowed delta is this external artifact. Upgrades require review,
    # not automatic blessing of an arbitrary replacement SHA.
    result = subprocess.run(['git', '-C', str(source), 'diff', '--quiet', BASE, '--', '.',
                             ':(exclude)contrib/kanban-resource-controller'],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    if result.returncode:
        raise ValueError('compatibility:source-baseline-changed')


def scan_processes():
    result = []
    for proc in psutil.process_iter():
        try:
            if proc.uids().effective != os.getuid() or proc.status() == psutil.STATUS_ZOMBIE:
                continue
            created = proc.create_time()
            argv = proc.cmdline()
            parent = proc.ppid()
            if proc.create_time() != created:
                raise ValueError('identity:process-changed-during-scan')
            result.append(Process(proc.pid, created, parent, argv))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            # Darwin reports EINVAL/AccessDenied when a process exits between
            # status() and KERN_PROCARGS2. Only a proven exit may be discarded.
            try:
                if proc.status() == psutil.STATUS_ZOMBIE:
                    continue
            except psutil.NoSuchProcess:
                continue
            # Cannot prove whether an unreadable process consumes capacity.
            raise ValueError(f'identity:unreadable-process:{proc.pid}') from exc
    return result


class Runtime:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        verify_source(settings.source)
        self.command = Command(store)
        self.reader = Command(Store(store.root / 'reader'))
        self.tracker = DrainTracker(store)
        self.env = {'HOME': str(settings.user_home), 'HERMES_HOME': str(settings.home),
                    'HERMES_KANBAN_HOME': str(settings.home),
                    'HERMES_BIN': str(settings.source / 'hermes'),
                    'PATH': str(settings.python.parent) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                    'PYTHONDONTWRITEBYTECODE': '1', 'TZ': 'UTC', 'LANG': 'C.UTF-8',
                    'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                    'VECLIB_MAXIMUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'}

    def argv(self, *args):
        return [str(self.settings.python), '-B', str(self.settings.source / 'hermes'), *args]

    def read_json(self, argv, env=None):
        self.reader.start(argv, self.env if env is None else env, self.store.root, {'kind': 'read-only'})
        result = self.reader.wait(15)
        self.reader.store.remove('pending.json')
        return result

    def config(self):
        return self.read_json([str(self.settings.python), '-B', str(Path(__file__).with_name('config_probe.py')),
                               str(self.settings.source)])

    def boards(self):
        # `boards list` opens/migrates databases merely to print counts. Give it
        # metadata-only replicas, never live DB paths, even in observation mode.
        with tempfile.TemporaryDirectory(prefix='enumerate-', dir=self.store.root) as tmp:
            mirror = Path(tmp)
            root = self.settings.home / 'kanban' / 'boards'
            if root.exists():
                for board in root.iterdir():
                    if not board.is_dir() or not SLUG.fullmatch(board.name):
                        continue
                    metadata = board / 'board.json'
                    if not metadata.exists() and not (board / 'kanban.db').exists():
                        continue
                    value = json.loads(metadata.read_text()) if metadata.exists() else {}
                    if not isinstance(value, dict):
                        raise ValueError('compatibility:invalid-board-metadata')
                    target = mirror / 'kanban' / 'boards' / board.name
                    target.mkdir(parents=True, mode=0o700)
                    (target / 'board.json').write_text(json.dumps({'archived': value.get('archived', False)}))
            env = {**self.env, 'HOME': str(mirror), 'HERMES_HOME': str(mirror), 'HERMES_KANBAN_HOME': str(mirror)}
            result = self.read_json(self.argv('kanban', 'boards', 'list', '--json'), env)
            if not isinstance(result, list) or not all(isinstance(b, dict) and SLUG.fullmatch(b.get('slug', '')) for b in result):
                raise ValueError('compatibility:boards-list-contract')
            slugs = [b['slug'] for b in result]
            if len(slugs) != len(set(slugs)):
                raise ValueError('compatibility:duplicate-board')
            return slugs

    def execute(self, board, verb, task, before):
        verify_source(self.settings.source)
        if not SLUG.fullmatch(board):
            raise ValueError('invalid board')
        commands = {
            'dispatch': ['dispatch', '--max', '1', '--failure-limit', str(before['config']['failure_limit']), '--json'],
            'decompose': ['decompose', task, '--author', 'auto-decomposer', '--json'],
        }
        argv = self.argv('kanban', '--board', board, *commands[verb])
        self.store.write('launched.json', {'at': time.time(), 'board': board, 'verb': verb})
        self.command.start(argv, self.env, self.store.root, {'board': board, 'verb': verb, 'task': task, 'before': before})
        self.tracker.include([self.command.process.pid])
        try:
            return self.command.wait(120 if verb == 'decompose' else 30)
        finally:
            # The dispatcher may already have exited/reparented its worker.
            # Recover the exact spawned fingerprint from canonical events.
            db = (self.settings.home / 'kanban.db' if board == 'default' else
                  self.settings.home / 'kanban' / 'boards' / board / 'kanban.db')
            for run in read_board(db, board)['runs']:
                try:
                    process = psutil.Process(run['pid']) if run['pid'] else None
                    if process and round(process.create_time() * 100) == run['stamp']:
                        self.tracker.include([process.pid])
                except psutil.NoSuchProcess:
                    continue

    def drain(self):
        self.command.drain()
        self.reader.drain()
        self.tracker.refresh()

    def snapshot(self):
        verify_source(self.settings.source)
        cfg = self.config()
        boards, runs, holds = {}, [], config_holds(cfg)
        for slug in self.boards():
            db = (self.settings.home / 'kanban.db' if slug == 'default' else
                  self.settings.home / 'kanban' / 'boards' / slug / 'kanban.db')
            data = read_board(db, slug)
            boards[slug] = sorted(data['tasks'], key=lambda t: (-t['priority'], t['created_at'], t['id']))
            runs.extend(data['runs'])
            holds.extend(data['holds'])
        sys.path.insert(0, str(self.settings.source))
        try:
            from hermes_cli._parser import build_top_level_parser, PRE_ARGPARSE_INHERITED_FLAGS
            prefixes = []
            launchers = {self.env['HERMES_BIN'], str(self.settings.source / 'hermes'),
                         str(self.settings.python.parent / 'hermes')}
            interpreters = {str(self.settings.python), str(self.settings.python.with_name('python3')),
                            str(self.settings.python.resolve()), 'python3', 'python'}
            for interpreter in interpreters:
                prefixes.append([interpreter, '-m', 'hermes_cli.main'])
                for launcher in launchers:
                    prefixes.extend(([interpreter, launcher], [interpreter, '-B', launcher]))
            parser = WorkerParser(prefixes, build_top_level_parser()[0], PRE_ARGPARSE_INHERITED_FLAGS)
        finally:
            sys.path.pop(0)
        processes = scan_processes()
        inventory = reconcile(runs, processes, parser, time.time())
        self.tracker.include([w['pid'] for w in inventory['workers']])
        covered = {p for w in inventory['workers'] for p in [w['pid'], *w['descendants']]}
        if any(p['pid'] not in covered for p in self.tracker.known):
            holds.append('identity:tracked-descendants-still-live')
        holds.extend(inventory['holds'])
        try:
            (self.settings.home / 'ESTOP').lstat()
            estop = True
        except FileNotFoundError:
            estop = False
        except OSError:
            estop = True
        return {'config': cfg, 'boards': boards, 'runs': runs, 'workers': inventory['workers'],
                'holds': holds, 'estop': estop}
