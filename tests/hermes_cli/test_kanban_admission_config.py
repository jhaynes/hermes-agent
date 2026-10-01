"""Admission settings parsing against DEFAULT_CONFIG (plan rev2 §6).

The defaults are declared ONCE in ``DEFAULT_CONFIG["kanban"]["adaptive_admission"]``
and the parser falls back to them for every bad key — no duplicate literals
(anything else drifts into change-detector territory). A partial user block
deep-merges over those defaults.
"""

from __future__ import annotations

import pytest


DEFAULTS = {
    "mode": "off",
    "step": 2,
    "settle_seconds": 5,
    "backoff_cooldown_seconds": 10,
    "min_running": 2,
    "cpu_psi_hold": 30,
    "cpu_psi_backoff": 60,
    "headroom_min_gib": 8,
    "headroom_worker_multiple": 4,
}


def test_defaults_block_present_in_default_config():
    from hermes_cli.config import DEFAULT_CONFIG

    block = DEFAULT_CONFIG["kanban"]["adaptive_admission"]
    assert block == DEFAULTS


def test_parser_defaults_match_default_config():
    from hermes_cli import kanban_admission as ka
    from hermes_cli.config import DEFAULT_CONFIG

    parsed = ka.parse_admission_settings({"kanban": {}})
    for key, value in DEFAULT_CONFIG["kanban"]["adaptive_admission"].items():
        parsed_value = getattr(parsed, key)
        if key == "mode":
            assert parsed_value == value
        elif key == "settle_seconds":
            assert parsed_value == float(value)
        elif key in ("backoff_cooldown_seconds", "cpu_psi_hold", "cpu_psi_backoff"):
            assert parsed_value == float(value)
        else:
            assert parsed_value == value


def test_deep_merge_partial_block_over_defaults(tmp_path, monkeypatch):
    """A partial user block merges with defaults (deep merge, not replace)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import kanban_admission as ka
    from hermes_cli.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "kanban:\n"
        "  adaptive_admission:\n"
        "    mode: enforce\n"
        "    step: 3\n",
        encoding="utf-8",
    )
    cfg = load_config()
    block = cfg["kanban"]["adaptive_admission"]
    assert block["mode"] == "enforce"
    assert block["step"] == 3
    # Untouched keys kept their DEFAULT_CONFIG values.
    assert block["settle_seconds"] == DEFAULTS["settle_seconds"]
    assert block["cpu_psi_hold"] == DEFAULTS["cpu_psi_hold"]
    assert block["min_running"] == DEFAULTS["min_running"]

    parsed = ka.parse_admission_settings(cfg.get("kanban") or {})
    assert parsed.mode == "enforce"
    assert parsed.step == 3
    assert parsed.settle_seconds == 5.0
    assert parsed.cpu_psi_hold == 30.0
