"""Pressure-gated, paced kanban worker admission — the pure controller layer.

Behavior contracts for ``hermes_cli.kanban_admission`` (plan rev2, sections
5-6, 9-10). Everything here is table-driven with an injected clock and
signals; no test reads live ``/proc`` — the root-conftest autouse patch makes
``read_host_signals`` return all-None signals, so the default decision is
"inactive" and each test pins the signals it wants to classify.

The controller this file pins:

- GREEN (no CPU contention, memory headroom) admits ``step`` new workers,
  paced so at most one grant happens per ``settle_seconds`` (stamped at the
  DECISION time, not spawn completion).
- RED (CPU backoff threshold, or the MemAvailable tiers) admits zero and
  starts a cooldown; AMBER (CPU hold, or below the headroom floor) admits
  zero without a cooldown.
- A ``min_running`` liveness floor overrides CPU/headroom holds (never the
  MemAvailable tiers) so a quiet host never starves below it.
- Missing signals (macOS, unreadable /proc) mean "inactive": today's
  dispatcher, allowance None.
"""

from __future__ import annotations

import math

import pytest

from hermes_cli import kanban_admission as ka


GIB = 1024 * 1024 * 1024


def signals(
    *,
    cpu_psi=1.0,
    mem_psi=0.5,
    mem_avail=GIB * 40,
    mem_total=GIB * 62,
    mem_level="ok",
):
    """A GREEN-by-default HostSignals with every field overridable."""
    return ka.HostSignals(
        cpu_psi=cpu_psi,
        mem_psi=mem_psi,
        mem_avail_bytes=mem_avail,
        mem_total_bytes=mem_total,
        mem_level=mem_level,
        sampled_at=100.0,
    )


def settings(**over):
    base = dict(
        mode="enforce",
        step=2,
        settle_seconds=5.0,
        backoff_cooldown_seconds=10.0,
        min_running=2,
        cpu_psi_hold=30.0,
        cpu_psi_backoff=60.0,
        headroom_min_gib=8,
        headroom_worker_multiple=4,
    )
    base.update(over)
    return ka.AdmissionSettings(**base)


def controller(**cfg):
    return ka.AdmissionController(settings(**cfg))


@pytest.fixture(autouse=True)
def _pin_worker_bound(monkeypatch):
    """Deterministic headroom floor: a 4 GiB per-worker bound on a 62 GiB host
    -> 16 GiB floor (the tower shape from plan §5.5)."""
    monkeypatch.setattr(ka, "_worker_bound_cached", lambda: 4 * GIB)


def floor_gib(worker_bound_gib=4, mem_total_gib=62.5):
    return ka.headroom_floor_bytes(
        settings(), int(worker_bound_gib * GIB), int(mem_total_gib * GIB)
    )


# ---------------------------------------------------------------------------
# T24: headroom floor sizing
# ---------------------------------------------------------------------------


def test_headroom_floor_tower_shape():
    # 4 GiB bound, 62.5 GiB total -> max(8, 4*4)=16, clamped by 31.25 -> 16.
    assert floor_gib(4, 62.5) == 16 * GIB


def test_headroom_floor_clamped_to_half_total():
    # 4 GiB bound, 16 GiB total -> multiple says 16, half-total clamps to 8.
    assert floor_gib(4, 16) == 8 * GIB


def test_headroom_floor_minimum_binds_for_small_workers():
    # 1 GiB bound, 62.5 GiB total -> multiple says 4, min raises to 8.
    assert floor_gib(1, 62.5) == 8 * GIB


# ---------------------------------------------------------------------------
# T13: parse_psi_some_avg10
# ---------------------------------------------------------------------------


def test_parse_psi_real_fixture():
    line = "some avg10=2.31 avg60=1.02 avg300=0.78 total=1234567890 delta=5432"
    assert ka.parse_psi_some_avg10(line) == pytest.approx(2.31)


def test_parse_psi_full_line_is_not_the_some_value():
    # The "full" line describes total stall, not some-avg10; it must not parse.
    assert ka.parse_psi_some_avg10("full avg10=99.99 avg60=1.00 avg300=0.10") is None


def test_parse_psi_avg60_and_avg300_never_used():
    line = "some avg10=3.5 avg60=40.0 avg300=90.0 total=1 delta=2"
    assert ka.parse_psi_some_avg10(line) == pytest.approx(3.5)


def test_parse_psi_malformed_returns_none():
    for bad in ("", "garbage", "some avg10=abc avg60=1", "some", "some avg10=",
                "some avg10", "avg10=2.0", "some avg10=nan", "some avg10=inf",
                "some avg10=-1.5", "some avg10= 1.0"):
        assert ka.parse_psi_some_avg10(bad) is None


def test_parse_psi_oversized_input_returns_none():
    assert ka.parse_psi_some_avg10("some avg10=1.0 " * 500) is None


def test_parse_psi_never_raises_on_bytes():
    # Binary junk must not raise UnicodeDecodeError through the read path.
    assert ka.parse_psi_some_avg10("some avg10=1.0\x00\xff".encode("utf-8", "surrogateescape")) is None


# ---------------------------------------------------------------------------
# T1 / T2 / T14: pacing window
# ---------------------------------------------------------------------------


def test_green_grants_step_and_paces_from_decision_time():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(), settings=settings(), ceiling=64, host_running=10)
    assert d.level == "GREEN" and d.allowance == 2
    ctl.record(d.now, 1)
    # +4.0s: inside the settle window (5s, slack 0.5) -> held for pacing.
    assert ctl.decide(now=104.0, signals=signals(), settings=settings(),
                      ceiling=64, host_running=10).allowance == 0
    # +4.6s: inside PACING_SLACK -> granted again.
    assert ctl.decide(now=104.6, signals=signals(), settings=settings(),
                      ceiling=64, host_running=10).allowance == 2
    # +5.0s: window fully elapsed -> granted.
    assert ctl.decide(now=105.0, signals=signals(), settings=settings(),
                      ceiling=64, host_running=10).allowance == 2


def test_t1_delayed_spawn_row_window_stamped_at_decision():
    """A spawn that completes 1.1s after its decision must not push the next
    grant out: the window is stamped with the DECISION time."""
    ctl = controller()
    d = ctl.decide(now=200.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=0)
    ctl.record(d.now, 1)  # n=1, spawn "completes" at 201.1 — not recorded
    nxt = ctl.decide(now=205.0, signals=signals(), settings=settings(),
                     ceiling=64, host_running=5)
    assert nxt.allowance == 2, "grant must pace from the decision, not completion"


def test_t2_zero_spawn_does_not_start_window():
    ctl = controller()
    d = ctl.decide(now=300.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=5)
    ctl.record(d.now, 0)
    # Immediate next decision still grants: no window started.
    d2 = ctl.decide(now=300.5, signals=signals(), settings=settings(),
                    ceiling=64, host_running=5)
    assert d2.allowance == 2


def test_t14_fresh_controller_grants_exactly_step():
    ctl = controller()
    d = ctl.decide(now=0.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=0)
    assert d.allowance == 2, "restart must not inherit the ceiling as a grant"


# ---------------------------------------------------------------------------
# T3 / T4 / T4b: classification
# ---------------------------------------------------------------------------


def test_t3_cpu_hold_alone_is_amber_without_cooldown():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(cpu_psi=30.0), settings=settings(),
                   ceiling=64, host_running=5)
    assert (d.level, d.allowance) == ("AMBER", 0)
    assert d.trigger == "cpu_psi"
    # No cooldown started: GREEN at +1s still grants.
    d2 = ctl.decide(now=101.0, signals=signals(), settings=settings(),
                    ceiling=64, host_running=5)
    assert d2.allowance == 2


def test_t3_headroom_floor_alone_is_amber():
    ctl = controller()
    # Floor is 16 GiB on the tower shape; 16 GiB - 1 byte holds.
    sig = signals(mem_avail=16 * GIB - 1)
    d = ctl.decide(now=100.0, signals=sig, settings=settings(),
                   ceiling=64, host_running=5)
    assert (d.level, d.allowance, d.trigger) == ("AMBER", 0, "headroom")
    # Exactly at the floor does NOT hold (floor is a strict-below threshold).
    d2 = ctl.decide(now=100.5, signals=signals(mem_avail=16 * GIB),
                    settings=settings(), ceiling=64, host_running=5)
    assert d2.level == "GREEN"


def test_t4_each_red_signal_alone():
    cases = [
        ("cpu_psi at backoff", signals(cpu_psi=60.0), "cpu_psi"),
        ("mem_level elevated", signals(mem_level="elevated"), "mem_level"),
        ("mem_level critical", signals(mem_level="critical"), "mem_level"),
    ]
    for label, sig, trigger in cases:
        ctl = controller()
        d = ctl.decide(now=100.0, signals=sig, settings=settings(),
                       ceiling=64, host_running=5)
        assert (d.level, d.allowance) == ("RED", 0), label
        assert d.trigger == trigger, label
        # Cooldown started: GREEN 1s later is still held.
        d2 = ctl.decide(now=101.0, signals=signals(), settings=settings(),
                        ceiling=64, host_running=5)
        assert d2.allowance == 0 and d2.reason == "cooldown", label


def test_t4b_memory_psi_is_recorded_not_control():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(mem_psi=100.0),
                   settings=settings(), ceiling=64, host_running=5)
    assert d.level == "GREEN"
    assert d.allowance == 2
    assert d.signals["mem_psi"] == 100.0


# ---------------------------------------------------------------------------
# T5: cooldown
# ---------------------------------------------------------------------------


def test_t5_cooldown_blocks_then_clears():
    ctl = controller()
    ctl.decide(now=100.0, signals=signals(cpu_psi=80.0), settings=settings(),
               ceiling=64, host_running=5)
    # GREEN inside 10s cooldown -> 0, reason cooldown.
    d = ctl.decide(now=108.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=5)
    assert (d.allowance, d.reason) == (0, "cooldown")
    # At cooldown expiry -> step again.
    d2 = ctl.decide(now=110.0, signals=signals(), settings=settings(),
                    ceiling=64, host_running=5)
    assert d2.allowance == 2


def test_t5_cooldown_uses_red_timestamp_not_decide_time():
    ctl = controller()
    ctl.decide(now=100.0, signals=signals(cpu_psi=80.0), settings=settings(),
               ceiling=64, host_running=5)
    # A second RED later moves the cooldown anchor forward.
    ctl.decide(now=104.0, signals=signals(cpu_psi=80.0), settings=settings(),
               ceiling=64, host_running=5)
    d = ctl.decide(now=112.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=5)
    assert d.allowance == 0 and d.reason == "cooldown"


# ---------------------------------------------------------------------------
# T6: hysteresis between hold and backoff
# ---------------------------------------------------------------------------


def test_t6_oscillation_between_hold_and_backoff_never_admits():
    ctl = controller()
    now = 100.0
    for i in range(20):
        psi = 45.0 if i % 2 == 0 else 55.0
        d = ctl.decide(now=now, signals=signals(cpu_psi=psi),
                       settings=settings(), ceiling=64, host_running=5)
        assert d.level == "AMBER" and d.allowance == 0, f"decision {i}"
        assert d.reason != "cooldown", "AMBER must not start a cooldown"
        now += 1.0


# ---------------------------------------------------------------------------
# T8 / T9: min_running liveness
# ---------------------------------------------------------------------------


def test_t8_min_running_overrides_cpu_red():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(cpu_psi=90.0),
                   settings=settings(min_running=3), ceiling=64, host_running=1)
    # CPU RED but the host is below min_running -> lift to 2.
    assert d.allowance == 2


def test_t8_min_running_overrides_headroom_amber():
    ctl = controller()
    sig = signals(mem_avail=8 * GIB)  # below the 16 GiB floor -> AMBER
    d = ctl.decide(now=100.0, signals=sig, settings=settings(min_running=3),
                   ceiling=64, host_running=1)
    assert d.level == "AMBER"
    assert d.allowance == 2


def test_t8_min_running_never_overrides_mem_level_red():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(mem_level="critical"),
                   settings=settings(min_running=3), ceiling=64, host_running=0)
    assert d.allowance == 0, "the MemAvailable tiers can starve below min_running"


def test_t8_override_grant_larger_than_step_starts_window():
    ctl = controller()
    # Headroom AMBER (not RED): the override lifts WITHOUT arming the cooldown,
    # isolating the pacing window the override grant must start.
    sig = signals(mem_avail=8 * GIB)
    d = ctl.decide(now=100.0, signals=sig,
                   settings=settings(min_running=5), ceiling=64, host_running=1)
    assert d.level == "AMBER" and d.allowance == 4
    ctl.record(d.now, 4)
    d2 = ctl.decide(now=101.0, signals=signals(), settings=settings(),
                    ceiling=64, host_running=5)
    assert d2.allowance == 0 and d2.reason == "pacing"


def test_t9_override_skipped_when_host_count_unknown():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(cpu_psi=90.0),
                   settings=settings(min_running=3), ceiling=64,
                   host_running=None)
    assert d.allowance == 0, "a host-count error must not be treated as 0"


# ---------------------------------------------------------------------------
# T10 / T11: inactive semantics
# ---------------------------------------------------------------------------


def _inactive(case):
    ctl = controller()
    if case == "cpu_psi None":
        sig = signals(cpu_psi=None)
    elif case == "mem_avail None":
        sig = signals(mem_avail=None)
    elif case == "ceiling None":
        sig = signals()
    else:
        raise AssertionError(case)
    ceiling = None if case == "ceiling None" else 64
    return ctl.decide(now=100.0, signals=sig, settings=settings(),
                      ceiling=ceiling, host_running=5)


@pytest.mark.parametrize("case", ["cpu_psi None", "mem_avail None", "ceiling None"])
def test_t10_missing_signal_means_inactive(case):
    d = _inactive(case)
    assert d.allowance is None
    assert d.level == "INACTIVE"
    which = "cpu_psi" if case.startswith("cpu") else (
        "mem_avail" if case.startswith("mem_avail") else "ceiling")
    assert d.reason == f"admission_inactive:{which}", d.reason


def test_t10_mem_total_missing_with_mem_avail_present_is_inactive():
    ctl = controller()
    sig = signals(mem_total=None)
    d = ctl.decide(now=100.0, signals=sig, settings=settings(),
                   ceiling=64, host_running=5)
    assert d.allowance is None
    assert d.reason == "admission_inactive:mem_total"


def test_t11_macos_shaped_signals_inactive():
    ctl = controller()
    sig = ka.HostSignals(cpu_psi=None, mem_psi=None, mem_avail_bytes=None,
                         mem_total_bytes=None, mem_level="unknown", sampled_at=None)
    d = ctl.decide(now=100.0, signals=sig, settings=settings(),
                   ceiling=64, host_running=5)
    assert d.allowance is None and d.level == "INACTIVE"


def test_inactive_allows_the_ceiling_none_uncapped_host():
    """ceiling None with full signals is inactive (today's uncapped bursts)."""
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(), settings=settings(),
                   ceiling=None, host_running=1)
    assert d.allowance is None and d.level == "INACTIVE"


# ---------------------------------------------------------------------------
# T12: settings validation (fail-safe)
# ---------------------------------------------------------------------------


def _kanban_cfg(**block):
    return {"kanban": {"adaptive_admission": block}}


def _parse(block):
    warnings: list[str] = []
    parsed = ka.parse_admission_settings(_kanban_cfg(**block)["kanban"], warn=warnings.append)
    return parsed, warnings


def test_t12_defaults_when_block_missing():
    parsed, warnings = _parse({})
    assert parsed.mode == "off" and parsed.step == 2
    assert parsed.settle_seconds == 5.0
    assert parsed.backoff_cooldown_seconds == 10.0
    assert parsed.min_running == 2
    assert parsed.cpu_psi_hold == 30.0 and parsed.cpu_psi_backoff == 60.0
    assert parsed.headroom_min_gib == 8 and parsed.headroom_worker_multiple == 4
    assert parsed == ka.parse_admission_settings({"kanban": {}})
    assert parsed == ka.parse_admission_settings({})


def test_t12_boolean_mode_rejected_not_enforce():
    parsed, warnings = _parse({"mode": True})
    assert parsed.mode == "off"
    assert warnings


def test_t12_bad_mode_value_falls_back():
    parsed, warnings = _parse({"mode": "sometimes"})
    assert parsed.mode == "off" and warnings


def test_t12_non_numeric_rejected():
    for key in ("step", "settle_seconds", "backoff_cooldown_seconds",
                "min_running", "cpu_psi_hold", "cpu_psi_backoff",
                "headroom_min_gib", "headroom_worker_multiple"):
        parsed, warnings = _parse({key: "many"})
        base = ka.parse_admission_settings({"kanban": {}})
        assert getattr(parsed, key) == getattr(base, key), key
        assert warnings, key


def test_t12_nan_inf_negative_rejected():
    for key in ("step", "settle_seconds", "backoff_cooldown_seconds",
                "min_running", "cpu_psi_hold", "cpu_psi_backoff",
                "headroom_min_gib", "headroom_worker_multiple"):
        for bad in (float("nan"), float("inf"), float("-inf"), -1, -0.5):
            parsed, warnings = _parse({key: bad})
            base = ka.parse_admission_settings({"kanban": {}})
            assert getattr(parsed, key) == getattr(base, key), (key, bad)
            assert warnings, (key, bad)


def test_t12_step_range():
    parsed, warnings = _parse({"step": 0})
    assert parsed.step == 2 and warnings
    parsed, warnings = _parse({"step": 65})
    assert parsed.step == 2 and warnings
    parsed, _ = _parse({"step": 64})
    assert parsed.step == 64
    parsed, _ = _parse({"step": 1})
    assert parsed.step == 1


def test_t12_settle_floor():
    parsed, warnings = _parse({"settle_seconds": 0.5})
    assert parsed.settle_seconds == 5.0 and warnings


def test_t12_min_running_range():
    parsed, warnings = _parse({"min_running": 65})
    assert parsed.min_running == 2 and warnings
    parsed, _ = _parse({"min_running": 64})
    assert parsed.min_running == 64


def test_t12_psi_threshold_range():
    # Warnings dedupe per distinct value process-wide; reset so each bad
    # value in this loop is asserted to warn (an earlier test may have used it).
    ka._WARNED_BAD_VALUES.clear()
    for key in ("cpu_psi_hold", "cpu_psi_backoff"):
        for bad in (101, -1, 1000):
            parsed, warnings = _parse({key: bad})
            base = ka.parse_admission_settings({"kanban": {}})
            assert getattr(parsed, key) == getattr(base, key), (key, bad)
            assert warnings, (key, bad)


def test_t12_hold_ge_backoff_reverts_both():
    parsed, warnings = _parse({"cpu_psi_hold": 60, "cpu_psi_backoff": 30})
    assert parsed.cpu_psi_hold == 30.0 and parsed.cpu_psi_backoff == 60.0
    assert warnings
    # Equal also reverts (hysteresis requires hold < backoff).
    parsed, warnings = _parse({"cpu_psi_hold": 45, "cpu_psi_backoff": 45})
    assert parsed.cpu_psi_hold == 30.0 and parsed.cpu_psi_backoff == 60.0
    assert warnings


def test_t12_partial_block_merges_with_defaults():
    parsed, warnings = _parse({"mode": "shadow", "step": 3})
    assert not warnings
    assert parsed.mode == "shadow" and parsed.step == 3
    # The untouched keys kept their defaults.
    assert parsed.settle_seconds == 5.0
    assert parsed.cpu_psi_hold == 30.0


def test_t12_min_running_clamped_to_ceiling_in_decide():
    # min_running above the ceiling clamps to the ceiling (a grant can never
    # exceed what the ceiling would admit).
    ctl = controller(min_running=10)
    d = ctl.decide(now=100.0, signals=signals(cpu_psi=90.0),
                   settings=settings(min_running=10), ceiling=4, host_running=0)
    assert d.allowance == 4


def test_t12_rev1_keys_ignored_with_warning():
    parsed, warnings = _parse({"enabled": True, "floor": 4,
                               "memory_psi_hold": 5, "memory_psi_backoff": 15})
    base = ka.parse_admission_settings({"kanban": {}})
    assert parsed == base
    assert len(warnings) >= 1


def test_t12_warning_once_per_distinct_bad_value():
    seen: list[str] = []
    warn = seen.append
    ka.parse_admission_settings(_kanban_cfg(step="x")["kanban"], warn=warn)
    ka.parse_admission_settings(_kanban_cfg(step="x")["kanban"], warn=warn)
    ka.parse_admission_settings(_kanban_cfg(step="y")["kanban"], warn=warn)
    # "x" warned once (second call suppressed), "y" warned once.
    assert len(seen) == 2


def test_t12_boolean_numbers_rejected():
    # YAML true parses to Python True, which is an int instance — must not
    # sneak through as 1.
    parsed, warnings = _parse({"step": True})
    assert parsed.step == 2 and warnings
    parsed, warnings = _parse({"settle_seconds": False})
    assert parsed.settle_seconds == 5.0 and warnings


def test_t12_min_running_ceiling_clamp_is_decide_time():
    """The clamp is applied against the DECISION's ceiling, so a controller
    used with different ceilings (boot snapshot vs live) clamps correctly."""
    ctl = controller(min_running=10)
    d = ctl.decide(now=100.0, signals=signals(cpu_psi=90.0),
                   settings=settings(min_running=10), ceiling=8, host_running=0)
    assert d.allowance == 8


# ---------------------------------------------------------------------------
# T29: shadow mode parity
# ---------------------------------------------------------------------------


def _decide_all_levels(ctl, mode):
    out = []
    for sig in (signals(), signals(cpu_psi=35.0), signals(cpu_psi=70.0),
                signals(mem_level="elevated"), signals(mem_avail=8 * GIB)):
        d = ctl.decide(now=100.0, signals=sig, settings=settings(mode=mode),
                       ceiling=64, host_running=5)
        out.append((d.level, d.trigger, d.reason))
    return out


def test_t29_shadow_classifies_identically_to_enforce():
    enforce = _decide_all_levels(controller(), "enforce")
    shadow = _decide_all_levels(controller(), "shadow")
    assert shadow == enforce


def test_t29_shadow_would_hold_counters_accumulate():
    ctl = controller(mode="shadow")
    for i in range(4):
        d = ctl.decide(now=100.0 + i * 5, signals=signals(cpu_psi=45.0),
                       settings=settings(mode="shadow"), ceiling=64,
                       host_running=5)
        ctl.record(d.now, 0)
    counters = ctl.shadow_counters()
    assert counters["decisions"] >= 4
    assert counters["would_hold_seconds"] >= 15.0
    assert counters["hold_by_trigger"].get("cpu_psi", 0) >= 15.0
    # GREEN decisions accumulate green seconds too.
    d = ctl.decide(now=125.0, signals=signals(), settings=settings(mode="shadow"),
                   ceiling=64, host_running=5)
    assert counters["hold_by_trigger"]["cpu_psi"] > 0


def test_t29_shadow_still_records_levels_for_status():
    ctl = controller(mode="shadow")
    d = ctl.decide(now=100.0, signals=signals(cpu_psi=70.0),
                   settings=settings(mode="shadow"), ceiling=64, host_running=5)
    assert d.level == "RED" and d.allowance == 0


# ---------------------------------------------------------------------------
# T31: status write rule
# ---------------------------------------------------------------------------


def test_t31_status_written_on_full_tick_and_level_changes_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    ctl = controller()
    st = ka.StatusWriter(tmp_path)
    writes: list[dict] = []
    real = ka.atomic_json_write

    def spy(path, payload, **kw):
        writes.append(payload)
        real(path, payload, **kw)

    monkeypatch.setattr(ka, "atomic_json_write", spy)
    from hermes_cli import kanban_db as kb

    # Full tick GREEN -> write 1.
    d = ctl.decide(now=100.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=5)
    st.record_full_tick(d, extra={"pid": 1})
    # Same-state sub-pass decisions -> no additional writes.
    for t in (105.0, 110.0):
        d = ctl.decide(now=t, signals=signals(), settings=settings(),
                       ceiling=64, host_running=5)
        st.maybe_write_transition(d)
    assert len(writes) == 1
    # Level change -> write.
    d = ctl.decide(now=115.0, signals=signals(cpu_psi=70.0),
                   settings=settings(), ceiling=64, host_running=5)
    st.maybe_write_transition(d)
    assert len(writes) == 2
    # Trigger change at the same level -> write.
    d = ctl.decide(now=116.0, signals=signals(mem_level="elevated"),
                   settings=settings(), ceiling=64, host_running=5)
    st.maybe_write_transition(d)
    assert len(writes) == 3
    # Next full tick (same level) -> write 4.
    d = ctl.decide(now=120.0, signals=signals(mem_level="elevated"),
                   settings=settings(), ceiling=64, host_running=5)
    st.record_full_tick(d, extra={"pid": 1})
    assert len(writes) == 4
    # sampled_at present in the payload.
    assert all("sampled_at" in w for w in writes)
    # Level/trigger fields ride along.
    assert writes[-1]["level"] == "RED"
    assert writes[-1]["trigger"] == "mem_level"


def test_t31_status_write_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ka, "atomic_json_write", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    st = ka.StatusWriter(tmp_path)
    d = controller().decide(now=100.0, signals=signals(), settings=settings(),
                            ceiling=64, host_running=5)
    st.record_full_tick(d, extra={})
    st.maybe_write_transition(d)


# ---------------------------------------------------------------------------
# Over-cap host drains, never signals
# ---------------------------------------------------------------------------


def test_over_cap_host_grants_zero_and_drains():
    ctl = controller()
    d = ctl.decide(now=100.0, signals=signals(), settings=settings(),
                   ceiling=64, host_running=70)
    assert d.allowance == 0
    assert d.reason in ("over_ceiling", "ceiling")
