from __future__ import annotations

from dataclasses import dataclass
import math

GIB = 1024**3


@dataclass(frozen=True)
class HostSample:
    monotonic: float
    load1: float
    cores: int
    available_bytes: int
    pressure: str
    page_in: int
    page_out: int


@dataclass(frozen=True)
class AdmissionDecision:
    eligible: bool
    reason: str
    healthy_seconds: float = 0.0


class AdmissionPolicy:
    """Fail-closed resource policy with a continuous recovery dwell."""

    def __init__(self, recovery_seconds: float = 120, max_sample_gap: float = 35) -> None:
        self.recovery_seconds = recovery_seconds
        self.max_sample_gap = max_sample_gap
        self._previous: HostSample | None = None
        self._healthy_since: float | None = None

    def observe(self, current: HostSample) -> AdmissionDecision:
        previous = self._previous
        self._previous = current

        if not self._valid(current):
            self._healthy_since = None
            return AdmissionDecision(False, "unknown-telemetry")
        if previous is None:
            self._healthy_since = current.monotonic
            return AdmissionDecision(False, "paging-baseline")
        if current.monotonic <= previous.monotonic:
            self._healthy_since = None
            return AdmissionDecision(False, "nonmonotonic-time")
        if current.monotonic - previous.monotonic > self.max_sample_gap:
            self._healthy_since = current.monotonic
            return AdmissionDecision(False, "sample-gap")
        if current.page_in < previous.page_in or current.page_out < previous.page_out:
            self._healthy_since = None
            return AdmissionDecision(False, "paging-reset")
        if current.load1 >= current.cores:
            self._healthy_since = None
            return AdmissionDecision(False, "load")
        if current.pressure != "normal":
            self._healthy_since = None
            return AdmissionDecision(False, "pressure")
        if current.available_bytes < 4 * GIB:
            self._healthy_since = None
            return AdmissionDecision(False, "memory")
        if current.page_in > previous.page_in or current.page_out > previous.page_out:
            self._healthy_since = None
            return AdmissionDecision(False, "paging")
        if current.load1 > 0.8 * current.cores:
            self._healthy_since = None
            return AdmissionDecision(False, "recovery-load")
        if current.available_bytes < 5 * GIB:
            self._healthy_since = None
            return AdmissionDecision(False, "recovery-memory")

        if self._healthy_since is None:
            self._healthy_since = current.monotonic
        healthy_seconds = current.monotonic - self._healthy_since
        if healthy_seconds < self.recovery_seconds:
            return AdmissionDecision(False, "recovery-dwell", healthy_seconds)
        return AdmissionDecision(True, "eligible", healthy_seconds)

    def command_consumed(self, now: float) -> None:
        """Consume the current window, including failed or uncertain commands."""
        self._healthy_since = now

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
            and sample.pressure in {"normal", "warning", "critical"}
            and isinstance(sample.page_in, int)
            and not isinstance(sample.page_in, bool)
            and sample.page_in >= 0
            and isinstance(sample.page_out, int)
            and not isinstance(sample.page_out, bool)
            and sample.page_out >= 0
        )
