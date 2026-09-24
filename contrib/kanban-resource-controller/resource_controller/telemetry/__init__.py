from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Mapping

from . import constants
from .constants import MAX_DIAGNOSTIC_LEN, PRESSURE_VALUES


class TelemetryError(RuntimeError):
    """Raised for any telemetry read/parse/platform failure.

    Carries a machine-readable ``error_code`` (see ``constants.ERROR_*``)
    separate from the sanitized, single-line, <=300 char ``diagnostic``.
    """

    def __init__(self, error_code: str, diagnostic: str) -> None:
        clean = sanitize_diagnostic(diagnostic)
        super().__init__(f"{error_code}: {clean}")
        self.error_code = error_code
        self.diagnostic = clean


def sanitize_diagnostic(text: str) -> str:
    """Single-line, JSON-safe, <=300 chars; strips C0/C1 and format chars."""
    collapsed = "".join(
        " " if ch in "\r\n\t" else ch
        for ch in text
        if ch == "\n" or ch == "\r" or ch == "\t" or (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F))
    )
    collapsed = " ".join(collapsed.split())
    if len(collapsed) > MAX_DIAGNOSTIC_LEN:
        collapsed = collapsed[:MAX_DIAGNOSTIC_LEN]
    return collapsed


@dataclass(frozen=True)
class MemoryHealth:
    available_bytes: int
    swap_in_bytes: int
    swap_out_bytes: int
    counter_page_size_bytes: int
    pressure: str
    pressure_detail: Mapping[str, Any]
    source: str

    def __post_init__(self) -> None:
        if self.pressure not in PRESSURE_VALUES:
            raise TelemetryError(constants.ERROR_PARSE_ERROR, f"unknown pressure value {self.pressure!r}")


class TelemetryBackend:
    def sample(self) -> MemoryHealth:  # pragma: no cover - protocol
        raise NotImplementedError


def select_backend(platform: str | None = None, **kwargs) -> TelemetryBackend:
    """Return a backend instance for ``platform`` (default: sys.platform).

    Extra ``kwargs`` are forwarded to the backend constructor (used by tests
    to inject readers/runners).
    """
    plat = sys.platform if platform is None else platform
    if plat == "darwin":
        from .darwin import DarwinBackend

        return DarwinBackend(**kwargs)
    if plat.startswith("linux"):
        from .linux import LinuxBackend

        return LinuxBackend(**kwargs)
    raise TelemetryError(
        constants.ERROR_UNSUPPORTED_PLATFORM, f"unsupported platform: {plat}"
    )
