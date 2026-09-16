"""Portable host samples and opt-in, profile-scoped admission policy.

Swap occupancy alone is not active pressure. Load is primary; these per-process
limits cannot prevent swapping or recall work that has already started.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import TypeGuard

import psutil

logger = logging.getLogger(__name__)
_now = time.monotonic
_sample = None
_state_lock = threading.RLock()
_states = {}
_last_levels = {}


@dataclass(frozen=True)
class LoadSample:
    ts: float
    load1: float | None
    cores: int | None
    swap_used_pct: float | None


@dataclass(frozen=True)
class LoadPolicy:
    enabled: bool = False
    elevated_enter_ratio: float = 1.0
    elevated_exit_ratio: float = 0.8
    critical_enter_ratio: float = 2.0
    critical_exit_ratio: float = 1.5
    dwell_seconds: float = 120.0
    swap_critical_pct: float = 90.0
    elevated_cap_divisor: int = 2
    unbounded_elevated_cap: int = 2
    unbounded_critical_cap: int = 1


@dataclass(frozen=True)
class LoadState:
    level: str = 'ok'
    since_ts: float = 0.0


def _finite(value) -> TypeGuard[float | int]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def classify(sample: LoadSample | None, policy: LoadPolicy, state: LoadState) -> tuple[str, LoadState]:
    if not policy.enabled:
        return 'ok', LoadState()
    if (sample is None or not _finite(sample.cores) or sample.cores <= 0
            or not _finite(sample.load1) or sample.load1 < 0):
        return 'unknown', state
    ratio = sample.load1 / sample.cores
    swap = sample.swap_used_pct
    critical = ratio > policy.critical_enter_ratio or (
        ratio > policy.elevated_enter_ratio and _finite(swap) and policy.swap_critical_pct < swap <= 100)
    elapsed = sample.ts - state.since_ts
    if critical or (state.level == 'critical' and (
            ratio >= policy.critical_exit_ratio or elapsed < policy.dwell_seconds)):
        level = 'critical'
    elif ratio > policy.elevated_enter_ratio or (state.level == 'elevated' and (
            ratio >= policy.elevated_exit_ratio or elapsed < policy.dwell_seconds)):
        level = 'elevated'
    else:
        level = 'ok'
    return level, state if level == state.level else LoadState(level, sample.ts)


def validate_policy(raw) -> LoadPolicy:
    """Invalid fields default individually; inconsistent thresholds default together."""
    defaults = asdict(LoadPolicy())
    if not isinstance(raw, dict):
        logger.warning('system_load must be a mapping; using disabled defaults')
        return LoadPolicy()
    values = defaults.copy()
    for key, default in defaults.items():
        if key not in raw:
            continue
        value = raw[key]
        try:
            if key == 'enabled':
                if isinstance(value, str) and value.lower() in ('true', 'false'):
                    value = value.lower() == 'true'
                if not isinstance(value, bool):
                    raise ValueError
            else:
                if isinstance(value, bool):
                    raise ValueError
                value = float(value)
                if not math.isfinite(value) or value < 0 or (key != 'dwell_seconds' and value == 0):
                    raise ValueError
                if isinstance(default, int):
                    if not value.is_integer():
                        raise ValueError
                    value = int(value)
                if key == 'elevated_cap_divisor' and value < 2:
                    raise ValueError
                if key == 'swap_critical_pct' and value > 100:
                    raise ValueError
            values[key] = value
        except (ValueError, TypeError, OverflowError):
            logger.warning('Invalid system_load.%s; using default %r', key, default)
    if not (values['elevated_exit_ratio'] < values['elevated_enter_ratio'] < values['critical_enter_ratio']
            and values['elevated_exit_ratio'] < values['critical_exit_ratio'] < values['critical_enter_ratio']):
        logger.warning('Invalid system_load threshold ordering; using default thresholds')
        for key in ('elevated_exit_ratio', 'elevated_enter_ratio', 'critical_exit_ratio', 'critical_enter_ratio'):
            values[key] = defaults[key]
    return LoadPolicy(**values)


def _sysctl_swap() -> float | None:
    if sys.platform != 'darwin':
        return None
    try:
        # Disk-backed output bounds memory even if a broken probe floods stdout.
        with tempfile.TemporaryFile() as output:
            subprocess.run(['sysctl', '-n', 'vm.swapusage'], stdout=output,
                           stderr=subprocess.DEVNULL, timeout=2, check=True)
            output.seek(0)
            text = output.read(4096).decode('ascii', errors='replace')
        fields = dict((key, float(value) * (1024 ** 'KMGT'.index(unit)))
                      for key, value, unit in re.findall(r'(total|used)\s*=\s*([\d.]+)([KMGT])', text))
        if fields.get('total', 0) > 0:
            return 100 * fields['used'] / fields['total']
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    return None


def sample_system_load() -> LoadSample:
    """Cache raw host metrics for 30 monotonic seconds; fields fail independently."""
    global _sample
    now = _now()
    if _sample is not None and 0 <= now - _sample.ts < 30:
        return _sample
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        load1 = None
    try:
        cores = len(psutil.Process().cpu_affinity()) or os.cpu_count()
    except (AttributeError, OSError, psutil.Error):
        cores = os.cpu_count()
    try:
        swap = psutil.swap_memory().percent
    except (OSError, psutil.Error):
        swap = _sysctl_swap()
    _sample = LoadSample(now, load1, cores, swap)
    return _sample


@dataclass(frozen=True)
class LoadStatus:
    level: str
    effective_cap: int | None
    policy: LoadPolicy
    sample: LoadSample | None
    sample_age: float | None

    @property
    def pressured(self) -> bool:
        return self.level in ('elevated', 'critical')


def load_status(seam: str, base_cap: int | None, admission) -> LoadStatus:
    """Classify at the decision point; only raw samples are cached across homes.

    The lock serializes hysteresis and transition logs, not work execution.
    Failed config or metrics impose no additional restriction.
    """
    from hermes_constants import hermes_home_key
    from hermes_cli.config import load_config_readonly
    with _state_lock:
        key = (hermes_home_key(), seam)
        try:
            policy = validate_policy(load_config_readonly().get('system_load', {}))
        except Exception:
            policy = LoadPolicy()
        try:
            sample = sample_system_load() if policy.enabled else None
        except Exception:
            sample = None
        level, state = classify(sample, policy, _states.get(key, LoadState()))
        _states[key] = state
        cap = admission(base_cap, level, policy)
        age = max(0., _now() - sample.ts) if sample is not None else None
        previous = _last_levels.get(key, 'ok')
        if level != previous:
            logger.info('load-throttle: %s %s -> %s (%s%s; load1 %s/%s cores; swap %s%%; sample %ss old)',
                        seam, base_cap, cap, 'recovered -> ' if previous in ('elevated', 'critical') and level == 'ok' else '',
                        level, sample.load1 if sample else None, sample.cores if sample else None,
                        sample.swap_used_pct if sample else None, age)
        _last_levels[key] = level
        return LoadStatus(level, cap, policy, sample, age)
