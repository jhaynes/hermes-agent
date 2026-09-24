from __future__ import annotations

import os
import re
import subprocess
from typing import Callable

import psutil

from . import constants
from . import TelemetryError, MemoryHealth

_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_SWAPINS_RE = re.compile(r"^Swapins:\s+(\d+)\.$", re.MULTILINE)
_SWAPOUTS_RE = re.compile(r"^Swapouts:\s+(\d+)\.$", re.MULTILINE)

_VM_STAT_ARGV = ("/usr/bin/vm_stat",)
_SYSCTL_ARGV = ("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level")
_ENV = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}

DefaultRunner = Callable[[tuple], subprocess.CompletedProcess]


def _run(argv: tuple[str, ...]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_ENV,
            timeout=constants.DARWIN_TIMEOUT_SECONDS,
            check=False,
            close_fds=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise TelemetryError(constants.ERROR_VM_STAT_TIMEOUT, f"{argv[0]} timed out") from exc


def parse_vm_stat(stdout: bytes, stderr: bytes, returncode: int) -> tuple[int, int, int]:
    """Parse vm_stat output. Returns (page_size, swapins_pages, swapouts_pages)."""
    if returncode != 0:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, f"vm_stat exited {returncode}")
    if len(stdout) > constants.DARWIN_MAX_OUTPUT_BYTES or len(stderr) > constants.DARWIN_MAX_OUTPUT_BYTES:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat output oversized")
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat output undecodable") from exc

    page_matches = _PAGE_SIZE_RE.findall(text)
    if len(page_matches) != 1:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat page size header missing or duplicated")
    page_size = int(page_matches[0])
    if page_size <= 0 or (page_size & (page_size - 1)) != 0:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat page size not a power of two")
    if page_size < constants.MIN_PAGE_SIZE or page_size > constants.MAX_PAGE_SIZE:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat page size out of range")

    swapins = _SWAPINS_RE.findall(text)
    if len(swapins) != 1:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat Swapins line missing or duplicated")
    swapouts = _SWAPOUTS_RE.findall(text)
    if len(swapouts) != 1:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat Swapouts line missing or duplicated")

    return page_size, int(swapins[0]), int(swapouts[0])


def parse_pressure_level(stdout: bytes, returncode: int) -> int:
    if returncode != 0 or len(stdout) > constants.DARWIN_SYSCTL_MAX_OUTPUT_BYTES:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "native memory pressure unavailable")
    try:
        return int(stdout.strip())
    except ValueError as exc:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "native memory pressure malformed") from exc


class DarwinBackend:
    def __init__(
        self,
        *,
        runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess] = _run,
        available_memory: Callable[[], int] = lambda: int(psutil.virtual_memory().available),
    ) -> None:
        self._runner = runner
        self._available_memory = available_memory

    def sample(self) -> MemoryHealth:
        try:
            vm_result = self._runner(_VM_STAT_ARGV)
        except subprocess.TimeoutExpired as exc:
            raise TelemetryError(constants.ERROR_VM_STAT_TIMEOUT, "vm_stat timed out") from exc
        page_size, swapins_pages, swapouts_pages = parse_vm_stat(
            vm_result.stdout, vm_result.stderr, vm_result.returncode
        )
        swap_in_bytes = swapins_pages * page_size
        swap_out_bytes = swapouts_pages * page_size
        if swap_in_bytes >= constants.MAX_SAFE_BYTES or swap_out_bytes >= constants.MAX_SAFE_BYTES:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, "vm_stat byte value exceeds safe integer range")

        try:
            sysctl_result = self._runner(_SYSCTL_ARGV)
        except subprocess.TimeoutExpired as exc:
            raise TelemetryError(constants.ERROR_VM_STAT_TIMEOUT, "sysctl timed out") from exc
        level = parse_pressure_level(sysctl_result.stdout, sysctl_result.returncode)
        pressure = {1: constants.PRESSURE_NORMAL, 2: constants.PRESSURE_WARNING, 4: constants.PRESSURE_CRITICAL}.get(level)
        if pressure is None:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, f"unknown native pressure level {level}")

        available = int(self._available_memory())
        if available < 0:
            raise TelemetryError(constants.ERROR_READ_ERROR, "available memory negative")

        return MemoryHealth(
            available_bytes=available,
            swap_in_bytes=swap_in_bytes,
            swap_out_bytes=swap_out_bytes,
            counter_page_size_bytes=page_size,
            pressure=pressure,
            pressure_detail={"level": level},
            source=constants.SOURCE_DARWIN,
        )
