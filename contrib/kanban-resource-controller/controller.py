#!/usr/bin/env python3
"""External build-only admission service. Installation is deliberately separate."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from resource_controller.engine import Controller
from resource_controller.runtime import Runtime, Settings
from resource_controller.storage import Store, Singleton, check_private
from resource_controller.telemetry import sample_mac


def resume_admission(store):
    if store.read('pending.json') is not None or Store(store.root / 'reader').read('pending.json') is not None:
        raise ValueError('uncertain outcome requires offline operator reconciliation')
    if store.read('exit.json') is not None:
        raise ValueError('stop already requested; wait for natural drain')
    store.write('reset.json', {'at': time.time()})
    store.remove('hold.json')
    return {'requested': 'resume-after-fresh-dwell'}


def load_settings(path):
    check_private(path)
    data = json.loads(path.read_text())
    if set(data) != {'source', 'python', 'home', 'user_home', 'state'}:
        raise ValueError('controller config requires exactly the five explicit paths')
    return Settings(**{key: Path(value) for key, value in data.items()})


def safe_sample():
    try:
        return sample_mac()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None


def command_living(pending):
    import psutil
    if pending is None:
        return False
    if pending.get('pid') is None or pending.get('created') is None:
        return True  # Crash in launch window: human reconciliation required.
    try:
        proc = psutil.Process(pending['pid'])
        return proc.create_time() == pending['created'] and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def acknowledge(runtime, reason):
    store = runtime.store
    if not reason.strip() or store.read('hold.json') is None:
        raise ValueError('a manual hold and explicit reconciliation reason are required')
    pending = store.read('pending.json')
    reading = runtime.reader.store.read('pending.json')
    if command_living(pending) or command_living(reading) or not runtime.tracker.drained():
        raise ValueError('command/descendants still alive or launch identity unknown')
    # A proven-dead reader never mutates boards. Temporarily unblock it to
    # perform fresh canonical reconciliation, restoring its hold on failure.
    runtime.reader.store.remove('pending.json')
    try:
        snapshot = runtime.snapshot()
        if snapshot['workers'] or snapshot['holds']:
            raise ValueError('canonical/process compatibility holds remain')
    except Exception:
        if reading is not None:
            runtime.reader.store.write('pending.json', reading)
        raise
    store.write('acknowledgement.json', {'at': time.time(), 'reason': reason,
                                        'pending': pending, 'reader': reading})
    store.remove('pending.json')
    # Keep the hold; explicit resume still requires a fresh resource dwell.


def may_exit(runtime, tracker):
    runtime.drain()
    if not tracker.drained():
        return False
    if command_living(runtime.store.read('pending.json')) or command_living(runtime.reader.store.read('pending.json')):
        return False
    # Without a side-effecting command this service has never owned a worker
    # coalition. Unrelated host ambiguity must not prevent its harmless exit.
    if runtime.store.read('launched.json') is None and runtime.store.read('pending.json') is None:
        return True
    snapshot = runtime.snapshot()
    return not snapshot['workers'] and not snapshot['holds']


def supervise(runtime, controller):
    try:
        runtime.drain()
    except Exception as exc:
        controller.blocked('identity:supervision-unavailable', {'holds': [type(exc).__name__]})
        return False
    return True


def service_cycle(runtime, controller, next_tick):
    store, tracker = runtime.store, runtime.tracker
    if not supervise(runtime, controller):
        return False, next_tick
    if store.read('exit.json') is not None:
        try:
            drained = may_exit(runtime, tracker)
        except Exception:
            drained = False
        controller.status('manual:drained' if drained else 'manual:draining')
        if drained:
            store.remove('exit.json')
            return True, next_tick
    elif time.monotonic() >= next_tick:
        if store.read('reset.json') is not None:
            controller.gate.reset()
            store.remove('reset.json')
        status = controller.tick(safe_sample(), safe_sample)
        tracker.include([w['pid'] for w in status['workers']])
        next_tick = time.monotonic() + 30
    return False, next_tick


def serve(settings, store):
    with Singleton(store.root / '.service.lock'):
        runtime = Runtime(settings, store)
        controller = Controller(store, runtime, active=False)
        cfg = runtime.config()
        if cfg.get('dispatch_in_gateway') is not False:
            controller.status('compatibility:embedded-dispatch-enabled')
            return 2
        lock_path = settings.home / 'kanban' / '.dispatcher.lock'
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with Singleton(lock_path):
            controller.active = True
            tracker = runtime.tracker
            def stop_signal(_signum, _frame):
                store.write('hold.json', {'reason': 'signal drain', 'at': time.time()})
                store.write('exit.json', {'requested': True})
            signal.signal(signal.SIGTERM, stop_signal)
            signal.signal(signal.SIGINT, stop_signal)
            next_tick = 0
            warned = False
            while True:
                try:
                    finished, next_tick = service_cycle(runtime, controller, next_tick)
                    if finished:
                        return 0
                    warned = False
                except Exception as exc:
                    # Corrupt state / disk errors must not make launchd tear
                    # down a coalition that may still contain active work.
                    controller.gate.reset()
                    try:
                        controller.status('compatibility:state-unavailable', {'holds': [type(exc).__name__]})
                    except (OSError, ValueError):
                        if not warned:
                            print('controller state unavailable; admissions held; preserving supervision', file=sys.stderr)
                            warned = True
                time.sleep(1)


def request_hold(store, *, stop=False):
    store.write('hold.json', {'reason': 'operator admission-only hold', 'at': time.time()})
    if stop:
        store.write('exit.json', {'requested': True})
    return {'requested': 'stop' if stop else 'hold', 'workers_signalled': False}


def observe(settings, store):
    with Singleton(store.root / '.service.lock'):
        runtime = Runtime(settings, store)
        return Controller(store, runtime).tick(safe_sample())


def acknowledge_offline(settings, store, reason):
    with Singleton(store.root / '.service.lock'):
        with Singleton(settings.home / 'kanban' / '.dispatcher.lock'):
            acknowledge(Runtime(settings, store), reason)
    return {'acknowledged': True, 'manual_hold_retained': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--reason', default='')
    parser.add_argument('action', choices=['sample', 'observe', 'run', 'hold', 'stop', 'resume', 'acknowledge'])
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'sample':
        print(json.dumps(asdict(sample_mac())))
        return 0
    if args.config is None:
        parser.error('--config is required except for sample')
    settings = load_settings(args.config)
    store = Store(settings.state)
    handlers = {
        'acknowledge': lambda: acknowledge_offline(settings, store, args.reason),
        'resume': lambda: resume_admission(store),
        'hold': lambda: request_hold(store),
        'stop': lambda: request_hold(store, stop=True),
        'observe': lambda: observe(settings, store),
        'run': lambda: serve(settings, store),
    }
    result = handlers[args.action]()
    if isinstance(result, dict):
        print(json.dumps(result))
        return 0
    return result


if __name__ == '__main__':
    raise SystemExit(main())
