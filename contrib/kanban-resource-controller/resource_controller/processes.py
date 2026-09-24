from __future__ import annotations

import os

from .inventory import IdentityHold, ProcessSnapshot, parse_worker_argv
from .worker_identity import (
    IdentityUnstable,
    current_instantiation_epoch,
    current_process_start,
)


def scan_worker_processes(*, process_iter=None, start_fn=None) -> list[ProcessSnapshot]:
    """Read exact same-user worker identities through psutil.

    Each relevant process's start fingerprint is read immediately before and
    immediately after its environment is collected. If the two reads differ the
    whole identity (start + environment) is recaptured once; a second
    disagreement raises ``IdentityUnstable`` (MoA v1 decision 4). A process that
    vanishes mid-read is skipped here; if the board still names it, inventory
    reconciliation holds on the missing canonical worker.
    ``process_iter``/``start_fn`` are injectable for tests.
    """
    import psutil

    iterate = process_iter or psutil.process_iter
    read_start = start_fn or current_process_start
    snapshots: list[ProcessSnapshot] = []
    uid = os.getuid()
    for process in iterate(["pid", "ppid", "create_time", "cmdline", "uids"]):
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
            start_fingerprint, environment = _stable_identity(pid, process.environ, read_start)
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


def _stable_identity(pid: int, collect, read_start):
    """Bracket ``collect()`` (the environment read) between two start-fingerprint
    reads; one full recapture on disagreement, else ``IdentityUnstable``.
    Returns ``(start_fingerprint, collected)``; an unreadable start (``None`` on
    both reads) is returned as ``None`` so inventory raises ``identity-unavailable``."""
    for _attempt in range(2):
        before = read_start(pid)
        collected = collect()
        after = read_start(pid)
        if before == after:
            return after, collected
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
