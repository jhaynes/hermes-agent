"""Admission controller for the embedded kanban dispatcher (plan rev2, t_f3e3a34f).

Pressure-gated, paced ADMISSION under an operator ceiling: the running count
grows by at most ``step`` new workers per ``settle_seconds`` while the host
shows no CPU contention and has memory headroom, up to ``kanban.max_in_progress``.
It never kills or throttles running workers — a worker admitted while idle can
still start a 4 GiB test suite later; only the ceiling, the per-worker
MemoryMax and the kernel bound that growth.

This module is pure given its inputs (settings, signals, clock): the caller
(``gateway/kanban_watchers.py``) reads config and signals, calls
``AdmissionController.decide``, applies the returned allowance through
``dispatch_once(spawn_allowance=...)`` and reports back with ``record``. Modes
(§5.1):

- ``off`` — today's dispatcher, byte-for-byte: no sub-passes, no decisions, no
  status file.
- ``shadow`` — decisions computed and recorded, allowance NOT applied
  (``spawn_allowance=None``), no sub-pass spawns.
- ``enforce`` — allowance applied, sub-passes run every ``settle_seconds``.

Everything fails safe: unreadable /proc means INACTIVE (allowance ``None`` —
today's per-tick burst behavior, never a brick), bad config values warn once
and revert to their defaults, and the status file is observability only — it is
never read back for control.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from utils import atomic_json_write

# ---------------------------------------------------------------------------
# Module constants (plan §5.9): the only tuning values that are NOT config.
# ---------------------------------------------------------------------------

#: Half a 1 s sleep slice: a spawn that took 1.1 s or a slice boundary must
#: not push the next grant out a whole extra settle interval (§5.2, MoA B2).
PACING_SLACK = 0.5

#: Cap on one /proc/pressure read: the kernel line is ~120 bytes; anything
#: bigger is a wrong file, not a PSI report.
_PSI_READ_CAP = 4096

#: ``headroom_floor = min(max(headroom_min_gib, multiple x worker_bound),
#: mem_total / 2)`` — the /2 keeps a small host from getting a floor it can
#: never satisfy (§5.5).
_MEM_TOTAL_HALF_DIVISOR = 2

_GIB = 1024 * 1024 * 1024

#: Validation ranges (plan §6). Keys outside these revert to defaults with one
#: WARNING per distinct bad value.
_STEP_RANGE = (1, 64)
_MIN_RUNNING_RANGE = (1, 64)
_PSI_RANGE = (0.0, 100.0)
_SETTLE_FLOOR = 1.0

_MODES = ("off", "shadow", "enforce")

#: rev1 keys (and any other unknown key) get one WARNING and are ignored.
_KNOWN_KEYS = frozenset({
    "mode", "step", "settle_seconds", "backoff_cooldown_seconds",
    "min_running", "cpu_psi_hold", "cpu_psi_backoff",
    "headroom_min_gib", "headroom_worker_multiple",
})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class AdmissionSettings:
    """Validated ``kanban.adaptive_admission`` block (defaults = §5.9)."""

    mode: str = "off"
    step: int = 2
    settle_seconds: float = 5.0
    backoff_cooldown_seconds: float = 10.0
    min_running: int = 2
    cpu_psi_hold: float = 30.0
    cpu_psi_backoff: float = 60.0
    headroom_min_gib: int = 8
    headroom_worker_multiple: int = 4


# One WARNING per distinct bad value, process-wide (§6, MoA I7).
_WARNED_BAD_VALUES: set[tuple[str, str]] = set()


def _default_warn(message: str) -> None:
    import logging

    logging.getLogger("hermes_cli.kanban_admission").warning(message)


def _warn_bad(key: str, raw: Any, warn: Optional[Callable[[str], None]]) -> None:
    token = (key, repr(raw))
    if token in _WARNED_BAD_VALUES:
        return
    _WARNED_BAD_VALUES.add(token)
    (warn or _default_warn)(
        f"kanban adaptive_admission: invalid {key}={raw!r}; using the default "
        f"(see kanban.adaptive_admission in config.yaml)")


def _is_finite_number(value: Any) -> bool:
    """A real, non-bool, finite number (rejects NaN/inf/strings/booleans)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _number_field(
    block: dict, key: str, default: float, *,
    lo: Optional[float] = None, hi: Optional[float] = None,
    warn: Optional[Callable[[str], None]] = None,
    as_int: bool = False,
) -> float | int:
    raw = block.get(key)
    if raw is None:
        return default
    if not _is_finite_number(raw):
        _warn_bad(key, raw, warn)
        return default
    try:
        value = float(raw)
    except (OverflowError, ValueError):  # e.g. int too large for float (§6)
        _warn_bad(key, raw, warn)
        return default
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        _warn_bad(key, raw, warn)
        return default
    return int(value) if as_int else value


def parse_admission_settings(
    kanban_cfg: Optional[dict],
    *,
    warn: Optional[Callable[[str], None]] = None,
) -> AdmissionSettings:
    """Fail-safe parse of ``kanban.adaptive_admission`` (plan §6).

    A partial block merges over :class:`AdmissionSettings` defaults (which
    mirror ``DEFAULT_CONFIG`` — the single source). Every bad value warns once
    and reverts; YAML booleans for ``mode`` are rejected so a bare ``true``
    can never silently mean ``enforce``.
    """
    block: dict = {}
    if isinstance(kanban_cfg, dict):
        raw_block = kanban_cfg.get("adaptive_admission")
        if isinstance(raw_block, dict):
            block = raw_block
        elif raw_block is not None:
            _warn_bad("adaptive_admission", raw_block, warn)

    # Unknown keys warn once and are ignored (§6). Keys are stringified for
    # the warning and iterated WITHOUT sorting: a YAML mapping can carry
    # non-str keys (``1: oops``), and sorted() over mixed types raises
    # TypeError — the parse must be TOTAL so no config shape can kill the
    # dispatcher at boot or on the live re-read (R1 system F1).
    for key in set(block) - _KNOWN_KEYS:
        _warn_bad(key, block[key], warn)

    mode_raw = block.get("mode", "off")
    if isinstance(mode_raw, str) and mode_raw in _MODES:
        mode = mode_raw
    else:
        _warn_bad("mode", mode_raw, warn)
        mode = "off"

    hold = float(_number_field(
        block, "cpu_psi_hold", 30.0, lo=_PSI_RANGE[0], hi=_PSI_RANGE[1], warn=warn))
    backoff = float(_number_field(
        block, "cpu_psi_backoff", 60.0, lo=_PSI_RANGE[0], hi=_PSI_RANGE[1], warn=warn))
    # Hysteresis requires hold < backoff; a reversed pair reverts BOTH (§6).
    if not hold < backoff:
        _warn_bad("cpu_psi_hold", block.get("cpu_psi_hold"), warn)
        _warn_bad("cpu_psi_backoff", block.get("cpu_psi_backoff"), warn)
        hold, backoff = 30.0, 60.0

    return AdmissionSettings(
        mode=mode,
        step=int(_number_field(
            block, "step", 2, lo=_STEP_RANGE[0], hi=_STEP_RANGE[1],
            warn=warn, as_int=True)),
        settle_seconds=float(_number_field(
            block, "settle_seconds", 5.0, lo=_SETTLE_FLOOR, warn=warn)),
        backoff_cooldown_seconds=float(_number_field(
            block, "backoff_cooldown_seconds", 10.0, lo=0.0, warn=warn)),
        min_running=int(_number_field(
            block, "min_running", 2, lo=_MIN_RUNNING_RANGE[0],
            hi=_MIN_RUNNING_RANGE[1], warn=warn, as_int=True)),
        cpu_psi_hold=hold,
        cpu_psi_backoff=backoff,
        headroom_min_gib=int(_number_field(
            block, "headroom_min_gib", 8, lo=0, warn=warn, as_int=True)),
        headroom_worker_multiple=int(_number_field(
            block, "headroom_worker_multiple", 4, lo=0, warn=warn, as_int=True)),
    )


def live_admission_settings(
    *, warn: Optional[Callable[[str], None]] = None,
) -> AdmissionSettings:
    """Settings re-read from live config (mtime-cached by the loader; a torn
    write returns the last-known-good config, §6)."""
    from hermes_cli.config import load_config_readonly

    cfg = load_config_readonly() or {}
    return parse_admission_settings(cfg.get("kanban") or {}, warn=warn)


def live_configured_ceiling() -> Optional[int]:
    """``kanban.max_in_progress`` as the config says RIGHT NOW (the boot value
    may differ while #117755 is unmerged — §7.1)."""
    from hermes_cli.kanban_db_dispatch import configured_max_in_progress

    return configured_max_in_progress()


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclass
class HostSignals:
    """One host sample. ``None`` fields mean "unreadable" (the macOS shape)."""

    cpu_psi: Optional[float] = None
    mem_psi: Optional[float] = None
    mem_avail_bytes: Optional[int] = None
    mem_total_bytes: Optional[int] = None
    mem_level: str = "unknown"
    sampled_at: Optional[float] = None


def parse_psi_some_avg10(text: Any) -> Optional[float]:
    """``some avg10`` percentage from one ``/proc/pressure/<res>`` read.

    Returns ``None`` for anything else — the ``full`` line, a missing
    ``avg10`` field, non-finite or negative values, oversized input,
    non-str input. Never raises.
    """
    if not isinstance(text, str) or not text or len(text) > _PSI_READ_CAP:
        return None
    if not text.startswith("some "):
        return None
    for chunk in text.split():
        if chunk.startswith("avg10="):
            try:
                value = float(chunk.partition("=")[2])
            except ValueError:
                return None
            return value if (math.isfinite(value) and value >= 0.0) else None
    return None


def _read_psi_avg10(path: str) -> Optional[float]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return parse_psi_some_avg10(handle.read(_PSI_READ_CAP))
    except OSError:
        return None


def read_host_signals() -> HostSignals:
    """Best-effort Linux host sample; never raises (§5.4, §9).

    CPU/memory PSI come from ``/proc/pressure/*`` (bounded reads); MemAvailable
    and MemTotal ride the dispatcher's existing ``_system_memory_sample`` so
    both features see the same numbers; ``mem_level`` reuses the existing
    ``classify_pressure`` tiers so admission matches the dashboard banner.
    """
    cpu_psi = _read_psi_avg10("/proc/pressure/cpu")
    mem_psi = _read_psi_avg10("/proc/pressure/memory")
    from hermes_cli.kanban_db_dispatch import (
        _memory_pressure_level, _system_memory_sample,
    )

    sample = _system_memory_sample() or {}

    def _kib_to_bytes(key: str) -> Optional[int]:
        raw = sample.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        return raw * 1024

    return HostSignals(
        cpu_psi=cpu_psi,
        mem_psi=mem_psi,
        mem_avail_bytes=_kib_to_bytes("mem_available_kib"),
        mem_total_bytes=_kib_to_bytes("mem_total_kib"),
        mem_level=_memory_pressure_level(sample),
        sampled_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def headroom_floor_bytes(
    settings: AdmissionSettings,
    worker_bound: int,
    mem_total: int,
) -> int:
    """``min(max(min_gib, multiple x worker_bound), mem_total / 2)`` (§5.5).

    ``worker_bound`` is the per-worker MemoryMax (``worker_memory_max_bytes()``)
    — suite-running workers actually reach it, so it sizes the floor for the
    case that bites rather than the idle median. The ``mem_total / 2`` clamp
    keeps a small host from getting a floor it can never satisfy.
    """
    multiple = max(0, settings.headroom_worker_multiple) * max(0, worker_bound)
    floor = max(settings.headroom_min_gib * _GIB, multiple)
    if mem_total > 0:
        floor = min(floor, mem_total // _MEM_TOTAL_HALF_DIVISOR)
    return max(floor, 0)


_worker_bound_cache: dict[str, int] = {}


def _resolve_worker_bound() -> int:
    """The per-worker MemoryMax bound (public alias added by this change)."""
    from tools.process_registry import worker_memory_max_bytes

    return worker_memory_max_bytes()


def _worker_bound_cached() -> int:
    """``_resolve_worker_bound`` resolved once per process (§9)."""
    if "value" not in _worker_bound_cache:
        try:
            _worker_bound_cache["value"] = _resolve_worker_bound()
        except Exception:
            _worker_bound_cache["value"] = 0
    return _worker_bound_cache["value"]


# ---------------------------------------------------------------------------
# Decision + controller
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """One admission decision (pure data; the loop applies it or not)."""

    now: float
    level: str  # GREEN | AMBER | RED | INACTIVE
    allowance: Optional[int]  # None = today's behavior (uncapped/inactive)
    reason: str
    trigger: Optional[str] = None
    ceiling: Optional[int] = None
    host_running: Optional[int] = None
    signals: dict = field(default_factory=dict)
    headroom_floor_bytes: Optional[int] = None


def classify(
    signals: HostSignals,
    settings: AdmissionSettings,
    floor: int,
) -> tuple[str, Optional[str]]:
    """``(level, trigger)`` from the control signals (§5.6).

    RED: the MemAvailable tiers (elevated/critical), or CPU at/over backoff.
    AMBER: MemAvailable below the headroom floor, or CPU at/over hold.
    Memory PSI is recorded but is NOT a control signal (§5.4: host PSI is
    hierarchical and counts reclaim inside a worker's own MemoryMax).
    """
    if signals.mem_level in ("elevated", "critical"):
        return "RED", "mem_level"
    if signals.cpu_psi is not None and signals.cpu_psi >= settings.cpu_psi_backoff:
        return "RED", "cpu_psi"
    if signals.mem_avail_bytes is not None and signals.mem_avail_bytes < floor:
        return "AMBER", "headroom"
    if signals.cpu_psi is not None and signals.cpu_psi >= settings.cpu_psi_hold:
        return "AMBER", "cpu_psi"
    return "GREEN", None


class AdmissionController:
    """Pacing + pressure state for one dispatcher process (§5.7).

    All timing is monotonic-clock-shaped but INJECTED via ``now``. State is
    process-local and not persisted: a restart starts clean and the first
    GREEN decision admits only ``step``.
    """

    def __init__(self, settings: AdmissionSettings):
        self.settings = settings
        self._last_grant_at: Optional[float] = None
        self._last_red_at: Optional[float] = None
        self._shadow = {
            "decisions": 0,
            "would_hold_seconds": 0.0,
            "level_seconds": {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0,
                              "INACTIVE": 0.0},
            "hold_by_trigger": {},
        }
        self._last_decision_at: Optional[float] = None

    def decide(
        self,
        now: float,
        signals: HostSignals,
        settings: AdmissionSettings,
        ceiling: Optional[int],
        host_running: Optional[int],
    ) -> Decision:
        """The control law (§5.6-§5.7): pure given inputs + this state.

        Missing inputs make the controller INACTIVE — allowance ``None``,
        i.e. today's behavior including today's per-tick burst up to the
        ceiling. There is no per-signal partial mode: the headroom floor
        can't be evaluated without MemAvailable, and running CPU-only would
        silently drop the memory guard (§5.4, MoA I7).
        """
        self.settings = settings
        level, trigger = "INACTIVE", None
        allowance: Optional[int] = None
        reason = "green"
        floor: Optional[int] = None

        if ceiling is None:
            reason = "admission_inactive:ceiling"
        elif signals.cpu_psi is None:
            reason = "admission_inactive:cpu_psi"
        elif signals.mem_avail_bytes is None:
            reason = "admission_inactive:mem_avail"
        elif signals.mem_total_bytes is None:
            reason = "admission_inactive:mem_total"
        else:
            floor = headroom_floor_bytes(
                settings, _worker_bound_cached(), signals.mem_total_bytes)
            level, trigger = classify(signals, settings, floor)
            if level == "RED":
                self._last_red_at = now
                allowance, reason = 0, "red"
            elif level == "AMBER":
                allowance, reason = 0, f"hold:{trigger}"
            else:
                allowance, reason = self._green_allowance(now, settings)
            if host_running is not None and host_running > ceiling:
                # Over-cap: nothing spawns, nothing is killed; the host
                # drains to the ceiling (§5.7).
                allowance = 0
                if level == "GREEN":
                    reason = "over_ceiling"
            if host_running is not None:
                allowance = self._min_running_override(
                    level, trigger, settings, ceiling, host_running, allowance)

        decision = Decision(
            now=now, level=level, allowance=allowance, reason=reason,
            trigger=trigger, ceiling=ceiling, host_running=host_running,
            signals={
                "cpu_psi": signals.cpu_psi,
                "mem_psi": signals.mem_psi,
                "mem_avail_bytes": signals.mem_avail_bytes,
                "mem_total_bytes": signals.mem_total_bytes,
                "mem_level": signals.mem_level,
                "sampled_at": signals.sampled_at,
            },
            headroom_floor_bytes=floor,
        )
        self._record_shadow(decision, now)
        return decision

    def _green_allowance(
        self, now: float, settings: AdmissionSettings,
    ) -> tuple[Optional[int], str]:
        """GREEN + cooldown + pacing -> step (§5.7 steps 3-5)."""
        if self._last_red_at is not None and \
                now - self._last_red_at < settings.backoff_cooldown_seconds:
            return 0, "cooldown"
        if self._last_grant_at is not None and \
                now - self._last_grant_at < settings.settle_seconds - PACING_SLACK:
            return 0, "pacing"
        return settings.step, "green"

    @staticmethod
    def _min_running_override(
        level: str,
        trigger: Optional[str],
        settings: AdmissionSettings,
        ceiling: Optional[int],
        host_running: int,
        allowance: Optional[int],
    ) -> Optional[int]:
        """Liveness floor (§5.7 step 6).

        CPU holds and the headroom floor never starve the host below
        ``min_running``; the MemAvailable tiers CAN (a genuinely exhausted
        host must drain). ``min_running`` above the ceiling clamps to the
        ceiling — a grant can never exceed what the ceiling would admit. An
        over-cap host (> ceiling >= min_running) never reaches this lift.
        """
        if level == "RED" and trigger == "mem_level":
            return allowance
        if host_running >= settings.min_running:
            return allowance
        floor_target = settings.min_running
        if ceiling is not None:
            floor_target = min(floor_target, ceiling)
        return max(allowance or 0, floor_target - host_running)

    # -- feedback ----------------------------------------------------------

    def record(self, decision_now: float, n: int) -> None:
        """Stamp a DECISION time (never spawn completion) when n >= 1 spawned
        (§5.7, MoA I6). A grant of 2 that spawned 1 still starts the window;
        a 0-spawn decision does not."""
        if n >= 1:
            self._last_grant_at = decision_now

    # -- shadow counters (§5.1, T29) ---------------------------------------

    def _record_shadow(self, decision: Decision, now: float) -> None:
        if self._last_decision_at is not None:
            dt = max(0.0, now - self._last_decision_at)
            self._shadow["level_seconds"][decision.level] = \
                self._shadow["level_seconds"].get(decision.level, 0.0) + dt
            if decision.allowance == 0:
                self._shadow["would_hold_seconds"] += dt
                if decision.trigger:
                    self._shadow["hold_by_trigger"][decision.trigger] = \
                        self._shadow["hold_by_trigger"].get(decision.trigger, 0.0) + dt
        self._last_decision_at = now
        self._shadow["decisions"] += 1

    def shadow_counters(self) -> dict:
        """Seconds per level/trigger + would-hold time, for the status file."""
        return {
            "decisions": self._shadow["decisions"],
            "would_hold_seconds": round(self._shadow["would_hold_seconds"], 3),
            "level_seconds": {k: round(v, 3)
                              for k, v in self._shadow["level_seconds"].items()},
            "hold_by_trigger": {k: round(v, 3)
                                for k, v in self._shadow["hold_by_trigger"].items()},
        }


def status_fields(
    decision: Decision,
    *,
    ceiling_configured: Optional[int] = None,
) -> dict:
    """Status-file field set (§9): observability only, never read for control."""
    ceiling = decision.ceiling
    return {
        "level": decision.level,
        "reason": decision.reason,
        "trigger": decision.trigger,
        "allowance": decision.allowance,
        "ceiling": ceiling,
        "ceiling_configured": ceiling_configured,
        "ceiling_restart_pending": bool(
            ceiling_configured is not None and ceiling is not None
            and ceiling != ceiling_configured
        ),
    }


class StatusWriter:
    """``<kanban_home>/kanban/admission_status.json`` (0644, no secrets).

    Write rule (§9, MoA I8): every full tick, plus a LEVEL or TRIGGER change
    — pacing/allowance flips don't write (~1/min + transitions).
    """

    def __init__(self, kanban_root: Path):
        self.path = Path(kanban_root) / "kanban" / "admission_status.json"
        self._last_written: Optional[tuple[str, Optional[str]]] = None

    def _write(self, payload: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(self.path, payload, mode=0o644)
        except Exception:
            pass

    def record_full_tick(self, decision: Decision, *, extra: dict) -> None:
        """Unconditional write (the full-tick heartbeat)."""
        self._write(self._payload(decision, extra))
        self._last_written = (decision.level, decision.trigger)

    def maybe_write_transition(self, decision: Decision) -> None:
        """Write only when LEVEL or TRIGGER changed since the last write."""
        key = (decision.level, decision.trigger)
        if self._last_written == key:
            return
        self._write(self._payload(decision, {}))
        self._last_written = key

    @staticmethod
    def _payload(decision: Decision, extra: dict) -> dict:
        payload = {
            "updated_at": time.time(),
            "sampled_at": decision.signals.get("sampled_at"),
            "level": decision.level,
            "reason": decision.reason,
            "trigger": decision.trigger,
            "allowance": decision.allowance,
            "ceiling": decision.ceiling,
            "host_running": decision.host_running,
            "headroom_floor_bytes": decision.headroom_floor_bytes,
            "signals": decision.signals,
        }
        payload.update(extra)
        return payload


# ---------------------------------------------------------------------------
# Exposure (§9): anchor line + coalesced transition/inactive logging.
# Pure helpers — the watcher wires them to its logger; nothing here reads
# config, the clock (beyond the injected ``now``) or the status file.
# ---------------------------------------------------------------------------


def admission_anchor_line(
    settings: AdmissionSettings, *, ceiling: Optional[int],
    signals: Optional[HostSignals] = None,
) -> str:
    """The one INFO anchor line per start (§9):

    ``kanban admission: mode=enforce ceiling=64 step=2/5s window=avg10
    headroom_floor=16.0GiB``. The floor is computed from ``signals`` when a
    sample is supplied; without one (unreadable host) the line reports the
    configured floor shape ``max(min_gib, multiple x worker_bound)`` as
    ``floor~...``. Returns ``""`` when the mode is ``off`` (no anchor for
    today's dispatcher, §5.1)."""
    if settings.mode == "off":
        return ""
    if (signals is not None and signals.mem_total_bytes
            and signals.mem_avail_bytes is not None):
        floor = headroom_floor_bytes(
            settings, _worker_bound_cached(), signals.mem_total_bytes)
        floor_txt = f"{floor / _GIB:.1f}GiB"
    else:
        floor_txt = (
            f"~max({settings.headroom_min_gib}GiB,"
            f"{settings.headroom_worker_multiple}xWorkerMax)"
        )
    return (
        f"kanban admission: mode={settings.mode} ceiling={ceiling} "
        f"step={settings.step}/{settings.settle_seconds:g}s window=avg10 "
        f"headroom_floor={floor_txt}"
    )


class AdmissionLogPolicy:
    """Coalesced transition logging (§9, R1 scope F2 / arch A4).

    Rules:
    - the FIRST RED after a non-RED run logs immediately at WARNING, with
      the trigger and threshold;
    - other transitions log at INFO at most once per full interval, with
      the latest state and a transition count;
    - ``admission_inactive`` (§5.4, MoA I7) logs at WARNING once per
      distinct reason while the mode isn't ``off``.

    Pure given the injected ``now``; the caller supplies the emit hooks.
    """

    def __init__(self, *, interval: float, warn, info):
        self._interval = max(float(interval), 1.0)
        self._warn = warn
        self._info = info
        self._level: Optional[str] = None
        self._reason: Optional[str] = None
        self._last_info_at: Optional[float] = None
        self._transitions = 0
        self._warned_inactive: set[str] = set()

    def observe(self, decision: Decision, *, now: float, mode: str) -> None:
        """One decision's log side effects (idempotent per state)."""
        if mode == "off":
            return
        reason = decision.reason
        if reason.startswith("admission_inactive:"):
            if reason not in self._warned_inactive:
                self._warned_inactive.add(reason)
                self._warn(
                    f"kanban admission: inactive ({reason}); the dispatcher "
                    f"runs today's uncapped behavior until inputs recover")
            return
        level = decision.level
        if self._level is None:
            self._level, self._reason = level, reason
            self._transitions = 0
            return
        if (level, reason) == (self._level, self._reason):
            return
        self._transitions += 1
        first_red = level == "RED" and self._level != "RED"
        self._level, self._reason = level, reason
        if first_red:
            self._warn(
                f"kanban admission: RED ({decision.trigger}); admitting "
                f"nothing until the condition clears")
            self._last_info_at = now
            return
        if self._last_info_at is None or now - self._last_info_at >= self._interval:
            self._info(
                f"kanban admission: {self._level} ({self._reason}); "
                f"{self._transitions} transition(s) since the last line")
            self._last_info_at = now
            self._transitions = 0
