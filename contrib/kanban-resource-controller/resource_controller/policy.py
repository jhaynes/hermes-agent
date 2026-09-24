from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .telemetry import constants

GIB = 1024**3


@dataclass(frozen=True)
class HostSample:
    monotonic: float
    load1: float
    cores: int
    available_bytes: int
    pressure: str
    swap_in: int
    swap_out: int
    counter_page_size_bytes: int = 0
    pressure_detail: Mapping[str, Any] | None = None
    source: str = ""


@dataclass(frozen=True)
class AdmissionDecision:
    eligible: bool
    reason: str
    healthy_seconds: float = 0.0
    observed_blockers: tuple[str, ...] = ()


class AdmissionPolicy:
    """Fail-closed resource policy with a continuous recovery dwell."""

    def __init__(self, recovery_seconds: float = 120, max_sample_gap: float = 35) -> None:
        self.recovery_seconds = recovery_seconds
        self.max_sample_gap = max_sample_gap
        self._previous: HostSample | None = None
        self._healthy_since: float | None = None

    def invalidate(self) -> None:
        """Called when telemetry fails; clears baseline/dwell without stale counters."""
        self._previous = None
        self._healthy_since = None

    def observe(self, current: HostSample) -> AdmissionDecision:
        previous = self._previous
        self._previous = current

        if not self._valid(current):
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_UNKNOWN_TELEMETRY)
        if previous is None:
            self._healthy_since = current.monotonic
            return AdmissionDecision(False, constants.REASON_SWAP_BASELINE)
        if current.monotonic <= previous.monotonic:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_NONMONOTONIC_TIME)
        if current.monotonic - previous.monotonic > self.max_sample_gap:
            self._healthy_since = current.monotonic
            return AdmissionDecision(False, constants.REASON_SAMPLE_GAP)
        if current.swap_in < previous.swap_in or current.swap_out < previous.swap_out:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_SWAP_RESET)

        blockers = self._observed_blockers(current, previous)

        if current.load1 >= current.cores:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_LOAD, observed_blockers=blockers)
        if current.pressure != constants.PRESSURE_NORMAL:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_PRESSURE, observed_blockers=blockers)
        if current.available_bytes < 4 * GIB:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_MEMORY, observed_blockers=blockers)
        # D1 = A: only swap-out growth gates; swap-in growth alone never holds.
        if current.swap_out > previous.swap_out:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_SWAP_OUT, observed_blockers=blockers)
        if current.load1 > 0.8 * current.cores:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_RECOVERY_LOAD, observed_blockers=blockers)
        if current.available_bytes < 5 * GIB:
            self._healthy_since = None
            return AdmissionDecision(False, constants.REASON_RECOVERY_MEMORY, observed_blockers=blockers)

        if self._healthy_since is None:
            self._healthy_since = current.monotonic
        healthy_seconds = current.monotonic - self._healthy_since
        if healthy_seconds < self.recovery_seconds:
            return AdmissionDecision(False, constants.REASON_RECOVERY_DWELL, healthy_seconds, observed_blockers=blockers)
        return AdmissionDecision(True, constants.REASON_ELIGIBLE, healthy_seconds, observed_blockers=blockers)

    def command_consumed(self, now: float) -> None:
        """Consume the current window, including failed or uncertain commands."""
        self._healthy_since = now

    @staticmethod
    def _observed_blockers(current: "HostSample", previous: "HostSample") -> tuple[str, ...]:
        blockers = []
        if current.load1 >= current.cores:
            blockers.append(constants.REASON_LOAD)
        if current.pressure != constants.PRESSURE_NORMAL:
            blockers.append(constants.REASON_PRESSURE)
        if current.available_bytes < 4 * GIB:
            blockers.append(constants.REASON_MEMORY)
        if current.swap_out > previous.swap_out:
            blockers.append(constants.REASON_SWAP_OUT)
        return tuple(blockers)

    @staticmethod
    def _valid(sample: HostSample) -> bool:
        return (
            math.isfinite(sample.monotonic)
            and math.isfinite(sample.load1)
            and sample.monotonic >= 0
            and sample.load1 >= 0
            and isinstance(sample.cores, int)
            and not isinstance(sample.cores, bool)
            and sample.cores > 0
            and isinstance(sample.available_bytes, int)
            and not isinstance(sample.available_bytes, bool)
            and sample.available_bytes >= 0
            and sample.pressure in constants.PRESSURE_VALUES
            and isinstance(sample.swap_in, int)
            and not isinstance(sample.swap_in, bool)
            and sample.swap_in >= 0
            and isinstance(sample.swap_out, int)
            and not isinstance(sample.swap_out, bool)
            and sample.swap_out >= 0
        )
