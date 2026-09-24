from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .spec import AdmissionCaps


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
    dispatch_profiles: tuple[str, ...]
    admission_caps: AdmissionCaps
    hermes_host_cap: int
    hermes_profile_cap: int

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        admission_caps: AdmissionCaps,
    ) -> "ControllerConfig":
        missing = _REQUIRED_KEYS - set(raw)
        unknown = set(raw) - _REQUIRED_KEYS - _ALLOWED_IGNORED_KEYS
        if missing:
            raise ConfigCompatibilityError(f"missing required key: {sorted(missing)[0]}")
        if unknown:
            raise ConfigCompatibilityError(f"unsupported kanban setting: {sorted(unknown)[0]}")
        _exact_bool(raw, "dispatch_in_gateway", False)
        _exact_int(raw, "max_in_progress", admission_caps.host_cap)
        _exact_int(
            raw,
            "max_in_progress_per_profile",
            admission_caps.maximum_profile_cap,
        )
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
        if (
            not isinstance(profiles, list)
            or not profiles
            or any(not isinstance(item, str) or not item.strip() for item in profiles)
        ):
            raise ConfigCompatibilityError("dispatch_profiles must be a nonempty string list")
        if len(set(profiles)) != len(profiles):
            raise ConfigCompatibilityError("dispatch_profiles must not contain duplicates")
        unknown_profiles = set(dict(admission_caps.profile_overrides)) - set(profiles)
        if unknown_profiles:
            raise ConfigCompatibilityError(
                f"dispatch_profiles missing override profile: {sorted(unknown_profiles)[0]}"
            )
        return cls(
            failure_limit=failure_limit,
            auto_decompose=auto_decompose,
            review_dispatch=review_dispatch,
            default_assignee=default_assignee,
            dispatch_profiles=tuple(profiles),
            admission_caps=admission_caps,
            hermes_host_cap=raw["max_in_progress"],
            hermes_profile_cap=raw["max_in_progress_per_profile"],
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
