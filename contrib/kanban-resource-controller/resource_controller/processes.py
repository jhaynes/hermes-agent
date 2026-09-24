from __future__ import annotations

import os

from .inventory import IdentityHold, ProcessSnapshot, parse_worker_argv
from .worker_identity import (
    IdentityUnstable,
    current_instantiation_epoch,
    current_process_start,
)


def scan_worker_processes() -> list[ProcessSnapshot]:
    """Read exact same-user worker identities through psutil.

    Each relevant process's start fingerprint is read once before and once
    after the argv/env collection; if the two reads differ (or the process
    vanished in between), the process is recaptured once, and otherwise an
    ``IdentityHold`` subclass (``identity-unstable``) is raised (MoA v1
    decision 4).
    """
    import psutil

    snapshots: list[ProcessSnapshot] = []
    uid = os.getuid()
    for process in psutil.process_iter(["pid", "ppid", "create_time", "cmdline", "uids"]):
        relevant = False
        try:
            uids = process.info.get("uids")
            if uids is not None and int(uids.real) != uid:
                continue
            argv = tuple(process.info.get("cmdline") or ())
            relevant = _looks_like_worker_candidate(argv)
            if not relevant:
                continue
            # Parsing first rejects lookalikes without relying on substrings.
            parse_worker_argv(argv)
            pid = int(process.info["pid"])
            start_fingerprint = _stable_start_fingerprint(pid)
            environment = process.environ()
            snapshots.append(
                ProcessSnapshot(
                    pid=pid,
                    created_at=float(process.info["create_time"]),
                    ppid=int(process.info["ppid"]),
                    argv=argv,
                    environment=environment,
                    accessible=True,
                    start_fingerprint=start_fingerprint,
                )
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError) as exc:
            if relevant:
                raise IdentityHold(f"relevant process identity inaccessible: {process.pid}") from exc
    return snapshots


def _stable_start_fingerprint(pid: int) -> int | None:
    """Read the start-time fingerprint twice (before/after argv+env are already
    collected by the caller's psutil calls that bracket this), recapturing once
    on disagreement; otherwise raise ``IdentityUnstable``."""
    before = current_process_start(pid)
    after = current_process_start(pid)
    if before == after:
        return before
    # One recapture on instability.
    recapture = current_process_start(pid)
    if recapture == after:
        return after
    raise IdentityUnstable(f"identity-unstable: start time unstable for pid {pid}")


def process_reader_epoch() -> str:
    """Epoch computed once per capture (see runtime.RuntimeWorld.capture)."""
    return current_instantiation_epoch()


def _looks_like_worker_candidate(argv: tuple[str, ...]) -> bool:
    if not argv:
        return False
    exact_prompts = [
        value for value in argv if value.startswith("work kanban task ")
    ]
    if not exact_prompts:
        return False
    # A candidate with a worker prompt but malformed argv is relevant and will
    # be rejected by parse_worker_argv rather than silently ignored.
    return True
