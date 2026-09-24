from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .telemetry import constants


class ConfigCompatibilityError(ValueError):
    pass


_REQUIRED_KEYS = {
    "dispatch_in_gateway",
    "max_in_progress",
    "max_in_progress_per_profile",
    "failure_limit",
    "auto_decompose",
    "reconcile_orphans",
    "dispatch_stale_timeout_seconds",
    "review_dispatch",
    "default_assignee",
    "dispatch_profiles",
}

# Pinned-baseline settings that do not change the supported dispatch/decompose
# contract enforced above. Future keys fail closed until reviewed.
_ALLOWED_IGNORED_KEYS = {
    "auto_subscribe_on_create",
    "notify_in_gateway",
    "dispatch_interval_seconds",
    "worker_log_rotate_bytes",
    "worker_log_backup_count",
    "orchestrator_profile",
    "auto_decompose_per_tick",
    "done_sub_retention_days",
    "max_spawn",
    "auto_promote_children",
}


@dataclass(frozen=True)
class ControllerConfig:
    failure_limit: int
    auto_decompose: bool
    review_dispatch: bool
    default_assignee: str | None
    dispatch_profiles: tuple[str, ...] | None
    linux_psi_some_avg10_warning: float
    linux_psi_full_avg10_critical: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ControllerConfig":
        missing = _REQUIRED_KEYS - set(raw)
        unknown = set(raw) - _REQUIRED_KEYS - _ALLOWED_IGNORED_KEYS - {"telemetry"}
        if missing:
            raise ConfigCompatibilityError(f"missing required key: {sorted(missing)[0]}")
        if unknown:
            raise ConfigCompatibilityError(f"unsupported kanban setting: {sorted(unknown)[0]}")
        _exact_bool(raw, "dispatch_in_gateway", False)
        _exact_int(raw, "max_in_progress", 2)
        _exact_int(raw, "max_in_progress_per_profile", 1)
        failure_limit = raw["failure_limit"]
        if not _is_int(failure_limit) or failure_limit <= 0:
            raise ConfigCompatibilityError("failure_limit must be a positive integer")
        auto_decompose = _bool(raw, "auto_decompose")
        _exact_bool(raw, "reconcile_orphans", True)
        _exact_int(raw, "dispatch_stale_timeout_seconds", 0)
        review_dispatch = _bool(raw, "review_dispatch")
        default_assignee = raw["default_assignee"]
        if default_assignee is not None and not isinstance(default_assignee, str):
            raise ConfigCompatibilityError("default_assignee must be a string or null")
        profiles = raw["dispatch_profiles"]
        if profiles is not None and (
            not isinstance(profiles, list)
            or any(not isinstance(item, str) or not item for item in profiles)
        ):
            raise ConfigCompatibilityError("dispatch_profiles must be a string list or null")
        some_warn, full_crit = _linux_psi(raw)
        return cls(
            failure_limit=failure_limit,
            auto_decompose=auto_decompose,
            review_dispatch=review_dispatch,
            default_assignee=default_assignee,
            dispatch_profiles=None if profiles is None else tuple(profiles),
            linux_psi_some_avg10_warning=some_warn,
            linux_psi_full_avg10_critical=full_crit,
        )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_int(raw: Mapping[str, Any], key: str, expected: int) -> None:
    value = raw[key]
    if not _is_int(value) or value != expected:
        raise ConfigCompatibilityError(f"{key} must be exactly {expected}")


def _bool(raw: Mapping[str, Any], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigCompatibilityError(f"{key} must be boolean")
    return value


def _exact_bool(raw: Mapping[str, Any], key: str, expected: bool) -> None:
    if _bool(raw, key) is not expected:
        raise ConfigCompatibilityError(f"{key} must be exactly {expected}")


def _linux_psi(raw: Mapping[str, Any]) -> tuple[float, float]:
    telemetry = raw.get("telemetry")
    if telemetry is None:
        return (
            constants.DEFAULT_LINUX_PSI_SOME_AVG10_WARNING,
            constants.DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL,
        )
    if not isinstance(telemetry, dict):
        raise ConfigCompatibilityError("telemetry must be an object")
    unknown_top = set(telemetry) - {"linux_psi"}
    if unknown_top:
        raise ConfigCompatibilityError(f"unsupported telemetry setting: {sorted(unknown_top)[0]}")
    linux_psi = telemetry.get("linux_psi")
    if linux_psi is None:
        return (
            constants.DEFAULT_LINUX_PSI_SOME_AVG10_WARNING,
            constants.DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL,
        )
    if not isinstance(linux_psi, dict):
        raise ConfigCompatibilityError("telemetry.linux_psi must be an object")
    allowed = {"some_avg10_warning", "full_avg10_critical"}
    unknown = set(linux_psi) - allowed
    if unknown:
        raise ConfigCompatibilityError(f"unsupported telemetry.linux_psi setting: {sorted(unknown)[0]}")
    some_warn = linux_psi.get("some_avg10_warning", constants.DEFAULT_LINUX_PSI_SOME_AVG10_WARNING)
    full_crit = linux_psi.get("full_avg10_critical", constants.DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL)
    for name, value in (("some_avg10_warning", some_warn), ("full_avg10_critical", full_crit)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigCompatibilityError(f"telemetry.linux_psi.{name} must be a finite number")
        if not math.isfinite(float(value)):
            raise ConfigCompatibilityError(f"telemetry.linux_psi.{name} must be finite")
        if not (0.0 <= float(value) <= 100.0):
            raise ConfigCompatibilityError(f"telemetry.linux_psi.{name} must be within [0, 100]")
    return float(some_warn), float(full_crit)
