from __future__ import annotations

import os

from .inventory import IdentityHold, ProcessSnapshot, parse_worker_argv


def scan_worker_processes() -> list[ProcessSnapshot]:
    """Read exact same-user worker identities through psutil."""
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
            environment = process.environ()
            snapshots.append(
                ProcessSnapshot(
                    pid=int(process.info["pid"]),
                    created_at=float(process.info["create_time"]),
                    ppid=int(process.info["ppid"]),
                    argv=argv,
                    environment=environment,
                    accessible=True,
                )
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError) as exc:
            if relevant:
                raise IdentityHold(f"relevant process identity inaccessible: {process.pid}") from exc
    return snapshots


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
