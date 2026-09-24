from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time
from typing import Sequence

from .cli_adapter import CliCommands
from .config import ConfigCompatibilityError
from .engine import ControllerEngine
from .host import HostSampler
from .locking import LockContended, SingletonLock
from .policy import AdmissionPolicy
from .preflight import read_kanban_config, verify_source_baseline
from .processes import scan_worker_processes
from .runtime import RuntimeWorld
from .spec import RuntimeSpec
from .storage import SecureStateStore
from .inventory import existing_capacity_violation
from .telemetry import TelemetryError, select_backend
from .telemetry import constants as telemetry_constants


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
    def config_reader():
        verify_source_baseline(spec.source_root, spec.expected_source_commit)
        return read_kanban_config(spec.hermes_home / "config.yaml")

    # PSI thresholds come from controller.json (RuntimeSpec), which is read once
    # at process start; changing them requires a controller restart.
    backend_kwargs: dict[str, float] = {}
    if sys.platform.startswith("linux"):
        backend_kwargs = {
            "some_avg10_warning": spec.linux_psi_some_avg10_warning,
            "full_avg10_critical": spec.linux_psi_full_avg10_critical,
        }
    sampler = HostSampler(backend=select_backend(**backend_kwargs))

    return RuntimeWorld(
        boards=spec.boards,
        admission_caps=spec.admission_caps,
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
    try:
        snapshot = _world(spec, store).capture()
    except ConfigCompatibilityError as exc:
        raw = read_kanban_config(spec.hermes_home / "config.yaml")
        print(json.dumps({
            "schema_version": telemetry_constants.SCHEMA_VERSION,
            "mode": "observation-only",
            "compatible": False,
            "error": str(exc),
            "expected_hermes": {
                "kanban.max_in_progress": spec.admission_caps.host_cap,
                "kanban.max_in_progress_per_profile": spec.admission_caps.maximum_profile_cap,
            },
            "observed_hermes": {
                "kanban.max_in_progress": raw.get("max_in_progress"),
                "kanban.max_in_progress_per_profile": raw.get("max_in_progress_per_profile"),
            },
        }, sort_keys=True))
        return 1
    violation = existing_capacity_violation(snapshot.workers, snapshot.config.admission_caps)
    print(json.dumps({
        "schema_version": telemetry_constants.SCHEMA_VERSION,
        "mode": "observation-only",
        "fingerprint": snapshot.fingerprint,
        "compatible": True,
        "estop": snapshot.estop is not None,
        "manual_hold": snapshot.manual_hold,
        "controller_reconciled_live_count": len(snapshot.workers),
        "boards": {
            board.board: {
                "hermes_db_running_count": board.hermes_db_running_count,
                "controller_reconciled_live_count": sum(
                    worker.board == board.board for worker in snapshot.workers
                ),
                "effective_cap": snapshot.config.admission_caps.for_board(board.board),
            }
            for board in snapshot.boards
        },
        "pacing": {
            "interval_seconds": spec.interval_seconds,
            "recovery_seconds": spec.recovery_seconds,
            "max_sample_gap_seconds": spec.max_sample_gap_seconds,
        },
        "admission": {
            "host_cap": snapshot.config.admission_caps.host_cap,
            "profile_cap": snapshot.config.admission_caps.profile_cap,
            "profile_overrides": dict(snapshot.config.admission_caps.profile_overrides),
            "board_cap": snapshot.config.admission_caps.board_cap,
            "board_overrides": dict(snapshot.config.admission_caps.board_overrides),
            "expected_hermes": {
                "kanban.max_in_progress": snapshot.config.admission_caps.host_cap,
                "kanban.max_in_progress_per_profile": snapshot.config.admission_caps.maximum_profile_cap,
            },
            "observed_hermes": {
                "kanban.max_in_progress": snapshot.config.hermes_host_cap,
                "kanban.max_in_progress_per_profile": snapshot.config.hermes_profile_cap,
            },
            "cap_violation": None if violation is None else {
                "reason": violation.reason,
                "name": violation.name,
                "count": violation.count,
                "cap": violation.cap,
            },
        },
        "unowned_subscriptions": snapshot.unowned_subscriptions,
        "identity": {
            "contract": "composite-only worker_started_at f'{epoch}|{start}', exact string match",
            "pinned_hermes_commit": "0e0a29ad315da6b6fd5b63e2903600af85e839e5",
            "workers_verified": len(snapshot.workers),
        },
        "sample": {
            "load1": snapshot.sample.load1,
            "cores": snapshot.sample.cores,
            "available_bytes": snapshot.sample.available_bytes,
            "pressure": snapshot.sample.pressure,
            "pressure_detail": dict(snapshot.sample.pressure_detail or {}),
            "swap_in_bytes": snapshot.sample.swap_in,
            "swap_out_bytes": snapshot.sample.swap_out,
            "counter_page_size_bytes": snapshot.sample.counter_page_size_bytes,
            "units": "bytes",
            "source": snapshot.sample.source,
        },
    }, sort_keys=True))
    return 0


def run_iteration(engine: ControllerEngine, world, store: SecureStateStore, *, stopping: bool) -> int | None:
    """One supervised loop iteration. Returns an exit code only when a drained stop completes."""
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
    except TelemetryError as exc:
        # Unknown gap: drop the policy baseline/dwell and the status delta baseline.
        engine.invalidate()
        store.write_json("status.json", {
            "schema_version": telemetry_constants.SCHEMA_VERSION,
            "mode": "best-effort downstream-first",
            "reason": telemetry_constants.REASON_TELEMETRY_ERROR,
            "error_code": exc.error_code,
            "diagnostic": exc.diagnostic,
            "manual_hold": _manual_hold(store),
        })
    except Exception as exc:
        store.write_json("status.json", {
            "mode": "best-effort downstream-first",
            "reason": "persistent-operator-hold",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        })
    return None


def admission_policy(spec: RuntimeSpec) -> AdmissionPolicy:
    """The only place pacing reaches the policy: values come from controller.json."""
    return AdmissionPolicy(
        recovery_seconds=spec.recovery_seconds,
        max_sample_gap=spec.max_sample_gap_seconds,
    )


def _run(spec: RuntimeSpec, store: SecureStateStore) -> int:
    world = _world(spec, store)
    engine = ControllerEngine(
        admission_policy(spec),
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
        log_line(f"started interval={spec.interval_seconds}s recovery={spec.recovery_seconds:g}s "
                 f"max_sample_gap={spec.max_sample_gap_seconds:g}s host_cap={spec.admission_caps.host_cap}")
        last: tuple | None = None
        while True:
            exit_code = run_iteration(engine, world, store, stopping=stopping)
            last = log_transition(store, last)
            if exit_code is not None:
                log_line(f"stopped exit={exit_code}")
                return exit_code
            time.sleep(spec.interval_seconds)


def log_line(message: str, *, stream=None) -> None:
    """One timestamped line to stdout (launchd routes it to logs/controller.log)."""
    out = sys.stdout if stream is None else stream
    out.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    out.flush()


def log_transition(store: SecureStateStore, last: tuple | None, *, stream=None) -> tuple | None:
    """Log every start and every change of blocking reason; stay quiet while unchanged."""
    try:
        status = store.read_json("status.json") or {}
    except Exception as exc:  # logging must never stop the loop
        log_line(f"status-unreadable {type(exc).__name__}", stream=stream)
        return last
    started = tuple(status.get("actual_task_ids") or ())
    key = (status.get("reason"), started, tuple(status.get("observed_blockers") or ()))
    if key != last:
        parts = [f"reason={key[0]}"]
        if started:
            parts.append("started=" + ",".join(started))
            if status.get("extra_starts"):
                parts.append(f"extra_starts={status['extra_starts']}")
        if key[2]:
            parts.append("blockers=" + ",".join(key[2]))
        for field in ("pressure", "error_code", "error_type"):
            if status.get(field) not in (None, "normal"):
                parts.append(f"{field}={status[field]}")
        log_line(" ".join(parts), stream=stream)
    return key


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
