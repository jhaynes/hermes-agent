from __future__ import annotations

import os
import time
from typing import Callable

from .policy import HostSample
from .telemetry import MemoryHealth, TelemetryBackend, TelemetryError, select_backend


def _logical_cores() -> int:
    import psutil

    value = psutil.cpu_count(logical=True)
    if value is None:
        raise TelemetryError("read-error", "logical CPU count unavailable")
    return int(value)


class HostSampler:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
        logical_cores: Callable[[], int] = _logical_cores,
        backend: TelemetryBackend | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._loadavg = loadavg
        self._logical_cores = logical_cores
        self._backend = backend if backend is not None else select_backend()

    def sample(self) -> HostSample:
        try:
            now = float(self._monotonic())
            load1 = float(self._loadavg()[0])
            cores = int(self._logical_cores())
            health: MemoryHealth = self._backend.sample()
        except TelemetryError:
            raise
        except Exception as exc:
            raise TelemetryError("read-error", f"telemetry sensor failed: {exc}") from exc

        result = HostSample(
            now,
            load1,
            cores,
            int(health.available_bytes),
            health.pressure,
            int(health.swap_in_bytes),
            int(health.swap_out_bytes),
            int(health.counter_page_size_bytes),
            dict(health.pressure_detail),
            health.source,
        )
        if not AdmissionShape.valid(result):
            raise TelemetryError("parse-error", "telemetry values invalid")
        return result


class AdmissionShape:
    @staticmethod
    def valid(sample: HostSample) -> bool:
        # AdmissionPolicy performs the authoritative finite/range check; this
        # early check keeps malformed native values out of snapshots.
        return sample.cores > 0 and sample.available_bytes >= 0 and sample.swap_in >= 0 and sample.swap_out >= 0
