from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time
from typing import Sequence

from .cli_adapter import CliCommands
from .engine import ControllerEngine
from .host import HostSampler
from .locking import LockContended, SingletonLock
from .policy import AdmissionPolicy
from .preflight import read_kanban_config, verify_source_baseline
from .processes import scan_worker_processes
from .runtime import RuntimeWorld
from .spec import RuntimeSpec
from .storage import SecureStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="External Hermes Kanban admission helper")
    parser.add_argument("action", choices=("check", "status", "hold", "resume", "run"))
    parser.add_argument("--config", required=True, help="Absolute path to mode-0600 runtime JSON")
    parser.add_argument("--reason", default="operator", help="Reason for hold/resume audit state")
    return parser


def set_manual_hold(store: SecureStateStore, engaged: bool, reason: str) -> None:
    store.write_json(
        "manual-hold.json",
        {"engaged": bool(engaged), "reason": reason, "changed_at": int(time.time())},
    )


def _manual_hold(store: SecureStateStore) -> bool:
    path = store.root / "manual-hold.json"
    if not path.exists():
        return False
    payload = store.read_json("manual-hold.json")
    return not isinstance(payload, dict) or payload.get("engaged") is not False


def _world(spec: RuntimeSpec, store: SecureStateStore) -> RuntimeWorld:
    sampler = HostSampler()

    def config_reader():
        verify_source_baseline(spec.source_root, spec.expected_source_commit)
        return read_kanban_config(spec.hermes_home / "config.yaml")

    return RuntimeWorld(
        boards=spec.boards,
        sampler=sampler.sample,
        config_reader=config_reader,
        process_reader=scan_worker_processes,
        estop_paths=(spec.hermes_home / "ESTOP",),
        manual_hold=lambda: _manual_hold(store),
    )


def _commands(spec: RuntimeSpec, world: RuntimeWorld) -> CliCommands:
    def reconcile_actual(board: str, _predicted: str) -> str | None:
        snapshot = world.capture()
        matching = [worker.task_id for worker in snapshot.workers if worker.board == board]
        if len(matching) > 1:
            raise RuntimeError("ambiguous post-command board workers")
        return matching[0] if matching else None

    return CliCommands(spec.hermes_executable, reconcile_actual=reconcile_actual)


def _check(spec: RuntimeSpec, store: SecureStateStore) -> int:
    snapshot = _world(spec, store).capture()
    print(json.dumps({
        "mode": "observation-only",
        "fingerprint": snapshot.fingerprint,
        "estop": snapshot.estop is not None,
        "manual_hold": snapshot.manual_hold,
        "workers": len(snapshot.workers),
        "boards": [board.board for board in snapshot.boards],
        "unowned_subscriptions": snapshot.unowned_subscriptions,
        "sample": {
            "load1": snapshot.sample.load1,
            "cores": snapshot.sample.cores,
            "available_bytes": snapshot.sample.available_bytes,
            "pressure": snapshot.sample.pressure,
            "page_in": snapshot.sample.page_in,
            "page_out": snapshot.sample.page_out,
        },
    }, sort_keys=True))
    return 0


def _run(spec: RuntimeSpec, store: SecureStateStore) -> int:
    world = _world(spec, store)
    engine = ControllerEngine(
        AdmissionPolicy(recovery_seconds=120, max_sample_gap=35),
        store,
        world,
        _commands(spec, world),
    )
    stopping = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        set_manual_hold(store, True, "service-stop-requested")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with SingletonLock(spec.dispatcher_lock):
        while True:
            try:
                if stopping:
                    snapshot = world.capture()
                    if not snapshot.workers:
                        return 0
                    store.write_json("status.json", {
                        "mode": "best-effort downstream-first",
                        "reason": "draining-descendants",
                        "workers": len(snapshot.workers),
                    })
                else:
                    engine.tick()
            except Exception as exc:
                store.write_json("status.json", {
                    "mode": "best-effort downstream-first",
                    "reason": "persistent-operator-hold",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                })
            time.sleep(spec.interval_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        raise SystemExit("--config must be absolute")
    spec = RuntimeSpec.read(config_path)
    store = SecureStateStore(spec.state_dir)
    if args.action == "status":
        try:
            print(json.dumps(store.read_json("status.json"), sort_keys=True))
            return 0
        except FileNotFoundError:
            print(json.dumps({"reason": "never-run"}))
            return 1
    if args.action == "hold":
        set_manual_hold(store, True, args.reason)
        return 0
    if args.action == "resume":
        set_manual_hold(store, False, args.reason)
        return 0
    if args.action == "check":
        return _check(spec, store)
    try:
        return _run(spec, store)
    except LockContended:
        print("dispatcher singleton lock is already owned", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
