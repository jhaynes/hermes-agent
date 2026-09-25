from __future__ import annotations

from typing import Callable, Mapping

from .storage import SecureStateStore
from .telemetry import sanitize_diagnostic


STUCK_REASONS = frozenset({"uncertain-outcome", "persistent-operator-hold"})
Notify = Callable[[dict[str, object]], None]


class StuckAlertTracker:
    """Durable one-shot tracker for an unbroken controller-local stuck incident."""

    def __init__(
        self,
        store: SecureStateStore,
        *,
        state_error: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.state_error = state_error or (lambda _message: None)

    def observe(
        self,
        status: Mapping[str, object],
        *,
        now: float,
        threshold: float,
        notify: Notify,
    ) -> None:
        details = _details(status)
        if details["reason"] not in STUCK_REASONS:
            self.store.write_json("alert-state.json", _inactive_state())
            return

        state = self._read_state()
        if not state["active"]:
            state = {
                "active": True,
                "started_at": float(now),
                "alert_attempted_at": None,
                **details,
            }
        else:
            state.update(details)
            if float(now) < float(state["started_at"]):
                state["started_at"] = float(now)
        self.store.write_json("alert-state.json", state)

        if state["alert_attempted_at"] is not None:
            return
        # started_at is never in the future here (reset above), so elapsed >= 0.
        elapsed = float(now) - float(state["started_at"])
        if elapsed < threshold:
            return

        state["alert_attempted_at"] = float(now)
        self.store.write_json("alert-state.json", state)
        try:
            notify(dict(details))
        except Exception as exc:
            self.state_error(
                sanitize_diagnostic(f"stuck notification callback failed: {type(exc).__name__}: {exc}")
            )

    def _read_state(self) -> dict[str, object]:
        try:
            payload = self.store.read_json("alert-state.json")
        except FileNotFoundError:
            return _inactive_state()
        except Exception as exc:
            self.state_error(
                sanitize_diagnostic(f"alert state unreadable: {type(exc).__name__}: {exc}")
            )
            return _inactive_state()
        if not _valid_state(payload):
            self.state_error("alert state malformed; starting a new incident")
            return _inactive_state()
        return dict(payload)


def _details(status: Mapping[str, object]) -> dict[str, object]:
    return {
        "reason": sanitize_diagnostic(str(status.get("reason") or "unknown")),
        "error_type": sanitize_diagnostic(str(status.get("error_type") or "")),
        "error": sanitize_diagnostic(str(status.get("error") or "")),
    }


def _inactive_state() -> dict[str, object]:
    return {
        "active": False,
        "started_at": None,
        "alert_attempted_at": None,
        "reason": "",
        "error_type": "",
        "error": "",
    }


def _valid_state(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != set(_inactive_state()):
        return False
    if not isinstance(payload.get("active"), bool):
        return False
    if payload["active"]:
        if not isinstance(payload.get("started_at"), (int, float)):
            return False
    elif payload.get("started_at") is not None:
        return False
    attempted = payload.get("alert_attempted_at")
    if attempted is not None and not isinstance(attempted, (int, float)):
        return False
    return all(isinstance(payload.get(name), str) for name in ("reason", "error_type", "error"))
