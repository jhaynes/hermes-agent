from __future__ import annotations

"""Single owner of the pinned Hermes worker-start-fingerprint contract.

Pinned to Hermes source 0e0a29ad315da6b6fd5b63e2903600af85e839e5:

- ``hermes_cli/kanban_db_dispatch.py::_process_fingerprint`` / ``_set_worker_pid``
  (the only writer of ``tasks.worker_started_at`` / ``task_runs.worker_started_at``):
  writes ``f"{current_instantiation_epoch()}|{start}"`` or the literal string
  ``"unverified"`` when the fingerprint capture fails. Never an integer, never NULL.
- ``gateway/drain_control.py::current_instantiation_epoch`` — the epoch half.
- ``gateway/status.py::_get_process_start_time`` — the start-time half.

Per the MoA v1 reconciliation (binding) in
plans/kanban-controller-worker-fingerprint-fix.md: composite-only, exact string
match, no legacy int/NULL support (no live writer produces them), no drift
tolerance. Every identity problem raises an ``IdentityHold`` subclass so callers
never see a bare ``ValueError``.
"""

from dataclasses import dataclass
import re
import sys
from pathlib import Path
from typing import Optional


class IdentityHold(RuntimeError):
    """Base: any problem establishing or verifying a worker's identity."""


class IdentityMalformed(IdentityHold):
    """Stored fingerprint does not match the composite grammar."""


class IdentityUnverified(IdentityHold):
    """Stored fingerprint is the literal ``"unverified"`` marker."""


class IdentityMismatch(IdentityHold):
    """Stored fingerprint does not equal the current process's fingerprint."""


class IdentityUnavailable(IdentityHold):
    """The current process's fingerprint could not be read."""


class IdentityUnsupportedPlatform(IdentityHold):
    """``sys.platform`` is neither ``darwin`` nor ``linux``."""


class IdentityUnstable(IdentityHold):
    """Fingerprint changed (or the process vanished) between the two reads."""


class IdentityConflict(IdentityHold):
    """``tasks`` and ``task_runs`` disagree on the fingerprint or pid."""


# Composite grammar (MoA v1 decision 1): exactly one '|', epoch <= 200 chars,
# start is a canonical decimal with no leading zeros, > 0. Length check happens
# before the regex.
_MAX_LEN = 1 + 200 + 1 + 20  # '|' + epoch + '|'-separator no, see below
_COMPOSITE_RE = re.compile(r"^(?P<epoch>[^|]{0,200})\|(?P<start>[1-9][0-9]{0,19})$")

UNVERIFIED_WORKER_FINGERPRINT = "unverified"


@dataclass(frozen=True)
class StoredFingerprint:
    epoch: str
    start: int
    raw: str


def parse_stored_fingerprint(value: object, *, context: str = "") -> StoredFingerprint:
    """Parse a ``worker_started_at`` column value under the composite-only contract.

    Anything that is not exactly ``^[^|]{0,200}\\|[1-9][0-9]{0,19}$`` raises an
    ``IdentityHold`` subclass naming ``context`` (typically ``task_id`` or
    ``task_id/run_id``). No legacy int / NULL support: no live writer produces
    them (MoA v1 decision 1).
    """
    label = f" for {context}" if context else ""
    if value is None:
        raise IdentityMalformed(f"identity-malformed: NULL worker_started_at{label}")
    if isinstance(value, bool):
        raise IdentityMalformed(f"identity-malformed: bool worker_started_at{label}")
    if isinstance(value, int):
        raise IdentityMalformed(f"identity-malformed: legacy integer worker_started_at{label}")
    if not isinstance(value, str):
        raise IdentityMalformed(f"identity-malformed: non-string worker_started_at{label}")
    if value == UNVERIFIED_WORKER_FINGERPRINT:
        raise IdentityUnverified(f"identity-unverified: worker_started_at unverified{label}")
    # Length bound before the regex: an oversized string must not be handed to it.
    if len(value) > 200 + 1 + 20:
        raise IdentityMalformed(f"identity-malformed: oversized worker_started_at{label}")
    match = _COMPOSITE_RE.fullmatch(value)
    if not match:
        raise IdentityMalformed(f"identity-malformed: not composite worker_started_at{label!r}: {value!r}")
    return StoredFingerprint(epoch=match.group("epoch"), start=int(match.group("start")), raw=value)


def current_instantiation_epoch(read_text=None) -> str:
    """Port of ``gateway.drain_control.current_instantiation_epoch``, line for line,
    including the partial-read cases: boot_id only gives ``"<id>:"``, pid1 only
    gives ``":<start>"``, neither gives ``""``. ``read_text`` is an injectable
    ``Path -> str`` reader for tests (fake ``/proc`` trees); defaults to reading
    the real filesystem.
    """
    reader = read_text or _default_read_text
    boot_id = pid1_start = ""
    try:
        boot_id = reader(Path("/proc/sys/kernel/random/boot_id")).strip()
    except OSError:
        pass
    try:
        # "<pid> (<comm>) <state> ...": comm may contain spaces/parens, so split on
        # the LAST ')'. starttime is field 22 (1-indexed) = tail index 19.
        pid1_start = reader(Path("/proc/1/stat")).rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    return f"{boot_id}:{pid1_start}" if (boot_id or pid1_start) else ""


def _default_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _linux_process_start(pid: int, read_text=None) -> Optional[int]:
    """Port of ``gateway.status._get_process_start_time``'s Linux branch: field 22
    of ``/proc/<pid>/stat`` via ``.split()[21]`` (NOT the rsplit-on-')' used by the
    epoch code above — this quirk is ported verbatim per MoA v1 decision 4, because
    matching the writer matters more than internal consistency)."""
    reader = read_text or _default_read_text
    try:
        return int(reader(Path(f"/proc/{pid}/stat")).split()[21])
    except (IndexError, ValueError, OSError):
        return None


def _darwin_process_start(pid: int, create_time_fn=None) -> Optional[int]:
    """Darwin: ``int(round(psutil.Process(pid).create_time() * 100))``."""
    try:
        if create_time_fn is not None:
            create_time = create_time_fn(pid)
        else:
            import psutil  # local import: not a runtime dependency at module load

            create_time = psutil.Process(pid).create_time()
        return int(round(float(create_time) * 100))
    except Exception:
        return None


def current_process_start(pid: int, *, read_text=None, create_time_fn=None) -> Optional[int]:
    """Platform-dispatching start-time fingerprint. Raises ``IdentityUnsupportedPlatform``
    outside darwin/linux (MoA v1 decision 3)."""
    if sys.platform == "linux":
        return _linux_process_start(pid, read_text=read_text)
    if sys.platform == "darwin":
        return _darwin_process_start(pid, create_time_fn=create_time_fn)
    raise IdentityUnsupportedPlatform(f"identity-unsupported-platform: {sys.platform!r}")


def current_fingerprint(
    pid: int, *, epoch: str, read_text=None, create_time_fn=None
) -> str:
    """The controller's own composite string for ``pid``, in the exact form the
    pinned Hermes ``_process_fingerprint`` writes: ``f"{epoch}|{start}"``.
    Raises ``IdentityUnavailable`` when the start time cannot be read."""
    start = current_process_start(pid, read_text=read_text, create_time_fn=create_time_fn)
    if start is None:
        raise IdentityUnavailable(f"identity-unavailable: cannot read start time for pid {pid}")
    return f"{epoch}|{start}"


def matches(stored: StoredFingerprint, current: str) -> bool:
    """Exact string equality of ``f"{epoch}|{start}"`` against the raw stored
    string, per ``_pid_recycled``'s composite branch (MoA v1 decision 2: no
    tolerance)."""
    return stored.raw == current


def require_match(stored_raw: object, current: str, *, context: str = "") -> None:
    """Parse ``stored_raw`` and assert it matches ``current``; raises the
    appropriate ``IdentityHold`` subclass otherwise."""
    stored = parse_stored_fingerprint(stored_raw, context=context)
    if not matches(stored, current):
        label = f" for {context}" if context else ""
        raise IdentityMismatch(f"identity-mismatch: PID reuse{label}")


def require_consistent(
    tasks_value: object, tasks_pid: object, runs_value: object, runs_pid: object, *, context: str = ""
) -> StoredFingerprint:
    """``tasks.worker_started_at``/``worker_pid`` must agree with the current
    run's ``task_runs`` row (MoA v1 decision 6: query scope). Any mismatch is
    ``identity-conflict``. Returns the parsed ``tasks`` fingerprint (the tasks
    row is canonical) once both parse and agree."""
    label = f" for {context}" if context else ""
    if tasks_value != runs_value or tasks_pid != runs_pid:
        raise IdentityConflict(
            f"identity-conflict: tasks vs task_runs disagree{label}"
        )
    return parse_stored_fingerprint(tasks_value, context=context)
