from __future__ import annotations

import os
import subprocess
import time
from typing import Callable

from .policy import HostSample


class TelemetryError(RuntimeError):
    pass


def _available_memory() -> int:
    import psutil

    return int(psutil.virtual_memory().available)


def _paging_counters() -> tuple[int, int]:
    import psutil

    swap = psutil.swap_memory()
    return int(swap.sin), int(swap.sout)


def _logical_cores() -> int:
    import psutil

    value = psutil.cpu_count(logical=True)
    if value is None:
        raise TelemetryError("logical CPU count unavailable")
    return int(value)


def _pressure_level() -> int:
    result = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1,
        check=False,
        close_fds=True,
    )
    if result.returncode != 0 or len(result.stdout) > 64:
        raise TelemetryError("native memory pressure unavailable")
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise TelemetryError("native memory pressure malformed") from exc


class HostSampler:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
        logical_cores: Callable[[], int] = _logical_cores,
        available_memory: Callable[[], int] = _available_memory,
        paging_counters: Callable[[], tuple[int, int]] = _paging_counters,
        pressure_level: Callable[[], int | None] = _pressure_level,
    ) -> None:
        self._monotonic = monotonic
        self._loadavg = loadavg
        self._logical_cores = logical_cores
        self._available_memory = available_memory
        self._paging_counters = paging_counters
        self._pressure_level = pressure_level

    def sample(self) -> HostSample:
        try:
            now = float(self._monotonic())
            load1 = float(self._loadavg()[0])
            cores = int(self._logical_cores())
            available = int(self._available_memory())
            page_in, page_out = self._paging_counters()
            pressure_raw = self._pressure_level()
        except TelemetryError:
            raise
        except Exception as exc:
            raise TelemetryError(f"telemetry sensor failed: {exc}") from exc
        if not isinstance(pressure_raw, int):
            raise TelemetryError(f"unknown native pressure level: {pressure_raw!r}")
        pressure = {1: "normal", 2: "warning", 4: "critical"}.get(pressure_raw)
        if pressure is None:
            raise TelemetryError(f"unknown native pressure level: {pressure_raw!r}")
        result = HostSample(
            now,
            load1,
            cores,
            available,
            pressure,
            int(page_in),
            int(page_out),
        )
        if not AdmissionShape.valid(result):
            raise TelemetryError("telemetry values invalid")
        return result


class AdmissionShape:
    @staticmethod
    def valid(sample: HostSample) -> bool:
        # AdmissionPolicy performs the authoritative finite/range check; this
        # early check keeps malformed native values out of snapshots.
        return sample.cores > 0 and sample.available_bytes >= 0 and sample.page_in >= 0 and sample.page_out >= 0
