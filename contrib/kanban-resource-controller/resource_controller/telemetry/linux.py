from __future__ import annotations

import errno
import os
import re
from typing import Callable

from . import constants
from . import TelemetryError, MemoryHealth

_PSWPIN_RE = re.compile(r"^pswpin\s+(\d+)$", re.MULTILINE)
_PSWPOUT_RE = re.compile(r"^pswpout\s+(\d+)$", re.MULTILINE)
_MEMAVAILABLE_RE = re.compile(r"^MemAvailable:\s+(\d+)\s+kB$", re.MULTILINE)
_FLOAT_RE = r"\d+\.\d+"
_PSI_LINE_RE = re.compile(rf"^(some|full)\s+(.*)$")
_PSI_TOKEN_RE = re.compile(r"([A-Za-z0-9_]+)=(\S+)")

_PSI_UNSUPPORTED_ERRNOS = frozenset(
    code for code in (getattr(errno, "EOPNOTSUPP", None), getattr(errno, "ENOTSUP", None)) if code is not None
)

VMSTAT_PATH = "/proc/vmstat"
MEMINFO_PATH = "/proc/meminfo"
PRESSURE_MEMORY_PATH = "/proc/pressure/memory"


def _read_capped(path: str) -> str:
    psi = path == PRESSURE_MEMORY_PATH
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError as exc:
        if psi:
            raise TelemetryError(constants.ERROR_PSI_UNAVAILABLE, f"{path} not found") from exc
        raise TelemetryError(constants.ERROR_READ_ERROR, f"{path} not found") from exc
    except OSError as exc:
        if psi and exc.errno in _PSI_UNSUPPORTED_ERRNOS:
            raise TelemetryError(constants.ERROR_PSI_UNAVAILABLE, f"{path} unsupported") from exc
        raise TelemetryError(constants.ERROR_READ_ERROR, f"{path} open failed: {exc.__class__.__name__}") from exc
    try:
        data = os.read(fd, constants.LINUX_READ_BYTES)
    except OSError as exc:
        if psi and exc.errno in _PSI_UNSUPPORTED_ERRNOS:
            raise TelemetryError(constants.ERROR_PSI_UNAVAILABLE, f"{path} unsupported") from exc
        raise TelemetryError(constants.ERROR_READ_ERROR, f"{path} read failed: {exc.__class__.__name__}") from exc
    finally:
        os.close(fd)
    if len(data) > constants.LINUX_READ_CAP_BYTES:
        raise TelemetryError(constants.ERROR_READ_ERROR, f"{path} oversized")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TelemetryError(constants.ERROR_READ_ERROR, f"{path} undecodable") from exc


def parse_vmstat(text: str) -> tuple[int, int]:
    pswpin = _PSWPIN_RE.findall(text)
    if len(pswpin) != 1:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "pswpin missing or duplicated")
    pswpout = _PSWPOUT_RE.findall(text)
    if len(pswpout) != 1:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "pswpout missing or duplicated")
    return int(pswpin[0]), int(pswpout[0])


def parse_meminfo(text: str) -> int:
    matches = _MEMAVAILABLE_RE.findall(text)
    if len(matches) != 1:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "MemAvailable missing or duplicated")
    return int(matches[0]) * 1024


def _finite_percent(value: str, field: str) -> float:
    if not re.fullmatch(_FLOAT_RE, value):
        raise TelemetryError(constants.ERROR_PARSE_ERROR, f"{field} not a well-formed float")
    parsed = float(value)
    if not (parsed == parsed) or parsed in (float("inf"), float("-inf")):
        raise TelemetryError(constants.ERROR_PARSE_ERROR, f"{field} not finite")
    if not (0.0 <= parsed <= 100.0):
        raise TelemetryError(constants.ERROR_PARSE_ERROR, f"{field} out of [0,100] range")
    return parsed


def parse_pressure_memory(text: str) -> dict:
    """Tolerant whitespace-split key=value grammar; 'some'/'full' each required once."""
    records: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        if kind not in ("some", "full"):
            continue
        tokens: dict[str, str] = {}
        for token in parts[1:]:
            match = _PSI_TOKEN_RE.fullmatch(token)
            if not match:
                continue
            key, value = match.group(1), match.group(2)
            if key in tokens:
                raise TelemetryError(constants.ERROR_PARSE_ERROR, f"duplicate PSI key {key} in {kind} record")
            tokens[key] = value
        if kind in records:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, f"duplicate PSI record {kind}")
        records[kind] = tokens

    if "some" not in records or "full" not in records:
        raise TelemetryError(constants.ERROR_PARSE_ERROR, "PSI missing some/full record")

    result: dict[str, float] = {}
    for kind in ("some", "full"):
        tokens = records[kind]
        for field in ("avg10", "avg60", "total"):
            if field not in tokens:
                raise TelemetryError(constants.ERROR_PARSE_ERROR, f"{kind} record missing {field}")
        result[f"{kind}_avg10"] = _finite_percent(tokens["avg10"], f"{kind}_avg10")
        result[f"{kind}_avg60"] = _finite_percent(tokens["avg60"], f"{kind}_avg60")
    return result


class LinuxBackend:
    def __init__(
        self,
        *,
        some_avg10_warning: float = constants.DEFAULT_LINUX_PSI_SOME_AVG10_WARNING,
        full_avg10_critical: float = constants.DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL,
        reader: Callable[[str], str] = _read_capped,
        page_size: Callable[[], int] | None = None,
    ) -> None:
        self._some_avg10_warning = some_avg10_warning
        self._full_avg10_critical = full_avg10_critical
        self._reader = reader
        self._page_size = page_size or (lambda: os.sysconf("SC_PAGE_SIZE"))

    def sample(self) -> MemoryHealth:
        try:
            page_size = int(self._page_size())
        except (OSError, ValueError) as exc:
            raise TelemetryError(constants.ERROR_READ_ERROR, f"SC_PAGE_SIZE unavailable: {exc.__class__.__name__}") from exc
        if page_size <= 0 or (page_size & (page_size - 1)) != 0:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, "SC_PAGE_SIZE not a power of two")
        if page_size < constants.MIN_PAGE_SIZE or page_size > constants.MAX_PAGE_SIZE:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, "SC_PAGE_SIZE out of range")

        vmstat_text = self._reader(VMSTAT_PATH)
        pswpin, pswpout = parse_vmstat(vmstat_text)
        swap_in_bytes = pswpin * page_size
        swap_out_bytes = pswpout * page_size
        if swap_in_bytes >= constants.MAX_SAFE_BYTES or swap_out_bytes >= constants.MAX_SAFE_BYTES:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, "vmstat byte value exceeds safe integer range")

        meminfo_text = self._reader(MEMINFO_PATH)
        available_bytes = parse_meminfo(meminfo_text)

        pressure_text = self._reader(PRESSURE_MEMORY_PATH)
        psi = parse_pressure_memory(pressure_text)

        if psi["full_avg10"] > self._full_avg10_critical:
            pressure = constants.PRESSURE_CRITICAL
        elif psi["some_avg10"] >= self._some_avg10_warning:
            pressure = constants.PRESSURE_WARNING
        else:
            pressure = constants.PRESSURE_NORMAL

        return MemoryHealth(
            available_bytes=available_bytes,
            swap_in_bytes=swap_in_bytes,
            swap_out_bytes=swap_out_bytes,
            counter_page_size_bytes=page_size,
            pressure=pressure,
            pressure_detail={
                "some_avg10": psi["some_avg10"],
                "some_avg60": psi["some_avg60"],
                "full_avg10": psi["full_avg10"],
                "full_avg60": psi["full_avg60"],
            },
            source=constants.SOURCE_LINUX,
        )
