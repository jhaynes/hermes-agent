from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .telemetry import constants


class SpecError(RuntimeError):
    pass


_KEYS = {
    "hermes_executable", "hermes_home", "source_root", "expected_source_commit",
    "dispatcher_lock", "state_dir", "boards", "interval_seconds",
}
_OPTIONAL_KEYS = {"telemetry", "admission", "pacing", "alerting"}
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ADMISSION_KEYS = {
    "host_cap", "profile_cap", "profile_overrides", "board_cap", "board_overrides",
}
DEFAULT_HOST_CAP, DEFAULT_PROFILE_CAP, DEFAULT_BOARD_CAP = 2, 1, 1
# Pacing defaults apply only when controller.json has no ``pacing`` section.
DEFAULT_RECOVERY_SECONDS, DEFAULT_MAX_SAMPLE_GAP_SECONDS = 120.0, 35.0
_PACING_KEYS = {"recovery_seconds", "max_sample_gap_seconds"}
DEFAULT_STUCK_ALERT_AFTER_SECONDS = 180.0
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 10.0
_ALERTING_KEYS = {"stuck_after_seconds", "notify_timeout_seconds"}
MAX_INTERVAL_SECONDS = 300


@dataclass(frozen=True)
class AdmissionCaps:
    host_cap: int
    profile_cap: int
    profile_overrides: tuple[tuple[str, int], ...]
    board_cap: int
    board_overrides: tuple[tuple[str, int], ...]

    def for_profile(self, profile: str) -> int:
        return dict(self.profile_overrides).get(profile, self.profile_cap)

    def for_board(self, board: str) -> int:
        return dict(self.board_overrides).get(board, self.board_cap)

    @property
    def maximum_profile_cap(self) -> int:
        return max((self.profile_cap, *(value for _, value in self.profile_overrides)))


@dataclass(frozen=True)
class RuntimeSpec:
    hermes_executable: Path
    hermes_home: Path
    source_root: Path
    expected_source_commit: str
    dispatcher_lock: Path
    state_dir: Path
    boards: Mapping[str, Path]
    interval_seconds: int
    admission_caps: AdmissionCaps
    linux_psi_some_avg10_warning: float = constants.DEFAULT_LINUX_PSI_SOME_AVG10_WARNING
    linux_psi_full_avg10_critical: float = constants.DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL
    recovery_seconds: float = DEFAULT_RECOVERY_SECONDS
    max_sample_gap_seconds: float = DEFAULT_MAX_SAMPLE_GAP_SECONDS
    stuck_alert_after_seconds: float = DEFAULT_STUCK_ALERT_AFTER_SECONDS
    notify_timeout_seconds: float = DEFAULT_NOTIFY_TIMEOUT_SECONDS

    @classmethod
    def read(cls, path: Path) -> "RuntimeSpec":
        if path.is_symlink():
            raise SpecError("runtime spec must not be a symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SpecError("runtime spec must be an owned regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SpecError("runtime spec mode must be 0600")
        if info.st_size > 1024 * 1024:
            raise SpecError("runtime spec is oversized")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpecError(f"runtime spec unreadable: {exc}") from exc
        if not isinstance(raw, dict) or not (_KEYS <= set(raw) <= _KEYS | _OPTIONAL_KEYS):
            raise SpecError("runtime spec keys do not match the pinned contract")
        paths = {
            key: _absolute(raw[key], key)
            for key in (
                "hermes_executable", "hermes_home", "source_root",
                "dispatcher_lock", "state_dir",
            )
        }
        commit = raw["expected_source_commit"]
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise SpecError("expected_source_commit must be a 40-character lowercase SHA")
        interval = raw["interval_seconds"]
        if (
            not isinstance(interval, int) or isinstance(interval, bool)
            or not 1 <= interval <= MAX_INTERVAL_SECONDS
        ):
            raise SpecError(f"interval_seconds must be an integer in [1, {MAX_INTERVAL_SECONDS}]")
        boards_raw = raw["boards"]
        if not isinstance(boards_raw, dict) or not boards_raw:
            raise SpecError("boards must be a nonempty object")
        boards: dict[str, Path] = {}
        for slug, value in boards_raw.items():
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                raise SpecError("board slug is invalid")
            boards[slug] = _absolute(value, f"boards.{slug}")
        try:
            paths["state_dir"].relative_to(paths["source_root"])
        except ValueError:
            pass
        else:
            raise SpecError("state_dir must be outside installed source")
        admission_caps = _admission_caps(
            raw.get("admission"), boards, present="admission" in raw,
        )
        some_warning, full_critical = _linux_psi(raw.get("telemetry"))
        recovery, sample_gap = _pacing(raw.get("pacing"), interval, present="pacing" in raw)
        stuck_alert_after, notify_timeout = _alerting(
            raw.get("alerting"), present="alerting" in raw,
        )
        return cls(
            paths["hermes_executable"],
            paths["hermes_home"],
            paths["source_root"],
            commit,
            paths["dispatcher_lock"],
            paths["state_dir"],
            boards,
            interval,
            admission_caps,
            some_warning,
            full_critical,
            recovery,
            sample_gap,
            stuck_alert_after,
            notify_timeout,
        )


def _admission_caps(
    admission: object,
    boards: Mapping[str, Path],
    *,
    present: bool,
) -> AdmissionCaps:
    if not present:
        return AdmissionCaps(
            DEFAULT_HOST_CAP, DEFAULT_PROFILE_CAP, (), DEFAULT_BOARD_CAP, (),
        )
    if not isinstance(admission, dict) or set(admission) != _ADMISSION_KEYS:
        raise SpecError("admission keys do not match the pinned contract")
    host_cap = _positive_int(admission["host_cap"], "admission.host_cap")
    profile_cap = _positive_int(admission["profile_cap"], "admission.profile_cap")
    board_cap = _positive_int(admission["board_cap"], "admission.board_cap")
    profile_overrides = _override_map(admission["profile_overrides"], "profile_overrides")
    board_overrides = _override_map(admission["board_overrides"], "board_overrides")
    if profile_cap > host_cap or any(value > host_cap for _, value in profile_overrides):
        raise SpecError("effective profile caps must not exceed admission.host_cap")
    unknown_boards = set(dict(board_overrides)) - set(boards)
    if unknown_boards:
        raise SpecError(f"unknown board override: {sorted(unknown_boards)[0]}")
    return AdmissionCaps(host_cap, profile_cap, profile_overrides, board_cap, board_overrides)


def _positive_int(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SpecError(f"{key} must be a positive integer")
    return value


def _override_map(value: object, key: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict):
        raise SpecError(f"admission.{key} must be an object")
    parsed: list[tuple[str, int]] = []
    for name, cap in value.items():
        if not isinstance(name, str) or not _SLUG.fullmatch(name):
            raise SpecError(f"admission.{key} name is invalid")
        parsed.append((name, _positive_int(cap, f"admission.{key}.{name}")))
    return tuple(sorted(parsed))


def _pacing(pacing: object, interval: int, *, present: bool) -> tuple[float, float]:
    """Optional controller.json ``pacing``; absent => the declared defaults above.

    ``recovery_seconds`` is the continuous healthy dwell required after any
    unhealthy sample or started command before the next start (0 disables it).
    ``max_sample_gap_seconds`` is the largest gap between samples that still
    counts as continuous; it must exceed ``interval_seconds``.
    """
    if not present:
        recovery, gap = DEFAULT_RECOVERY_SECONDS, DEFAULT_MAX_SAMPLE_GAP_SECONDS
    else:
        if not isinstance(pacing, dict) or set(pacing) != _PACING_KEYS:
            raise SpecError("pacing keys must be exactly recovery_seconds and max_sample_gap_seconds")
        recovery = _nonnegative_number(pacing["recovery_seconds"], "pacing.recovery_seconds")
        gap = _nonnegative_number(pacing["max_sample_gap_seconds"], "pacing.max_sample_gap_seconds")
    if gap <= interval:
        raise SpecError("pacing.max_sample_gap_seconds must exceed interval_seconds")
    return recovery, gap


def _nonnegative_number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{key} must be a number")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 3600.0:
        raise SpecError(f"{key} must be finite and within [0, 3600]")
    return value


def _alerting(alerting: object, *, present: bool) -> tuple[float, float]:
    if not present:
        return DEFAULT_STUCK_ALERT_AFTER_SECONDS, DEFAULT_NOTIFY_TIMEOUT_SECONDS
    if not isinstance(alerting, dict) or set(alerting) != _ALERTING_KEYS:
        raise SpecError(
            "alerting keys must be exactly stuck_after_seconds and notify_timeout_seconds"
        )
    stuck_after = _nonnegative_number(
        alerting["stuck_after_seconds"], "alerting.stuck_after_seconds",
    )
    notify_timeout = alerting["notify_timeout_seconds"]
    if isinstance(notify_timeout, bool) or not isinstance(notify_timeout, (int, float)):
        raise SpecError("alerting.notify_timeout_seconds must be a number")
    notify_timeout = float(notify_timeout)
    if not math.isfinite(notify_timeout) or not 1.0 <= notify_timeout <= 60.0:
        raise SpecError("alerting.notify_timeout_seconds must be finite and within [1, 60]")
    return stuck_after, notify_timeout


def _linux_psi(telemetry: object) -> tuple[float, float]:
    """Optional controller.json ``telemetry.linux_psi`` thresholds (D2); absent => defaults."""
    some = constants.DEFAULT_LINUX_PSI_SOME_AVG10_WARNING
    full = constants.DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL
    if telemetry is None:
        return some, full
    if not isinstance(telemetry, dict) or set(telemetry) - {"linux_psi"}:
        raise SpecError("telemetry must be an object with only linux_psi")
    linux_psi = telemetry.get("linux_psi")
    if linux_psi is None:
        return some, full
    allowed = {"some_avg10_warning", "full_avg10_critical"}
    if not isinstance(linux_psi, dict) or set(linux_psi) - allowed:
        raise SpecError("telemetry.linux_psi accepts only some_avg10_warning and full_avg10_critical")
    values = {"some_avg10_warning": some, "full_avg10_critical": full}
    for name in allowed & set(linux_psi):
        value = linux_psi[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(f"telemetry.linux_psi.{name} must be a number")
        value = float(value)
        if not math.isfinite(value) or not 0.0 <= value <= 100.0:
            raise SpecError(f"telemetry.linux_psi.{name} must be finite and within [0, 100]")
        values[name] = value
    return values["some_avg10_warning"], values["full_avg10_critical"]


def _absolute(value: object, key: str) -> Path:
    if not isinstance(value, str):
        raise SpecError(f"{key} must be a path string")
    path = Path(value)
    if not path.is_absolute():
        raise SpecError(f"{key} must be absolute")
    return path
