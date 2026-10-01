"""The embedded dispatcher's adaptive inter-tick wait (plan rev2 §5.2, §10).

Contracts for ``GatewayKanbanWatchersMixin._inter_tick_wait`` and the
sub-pass driver it hosts. All timing is INJECTED: a fake monotonic clock and
a sleep seam — no test waits on wall time.

The loop shape (plan §5.2):

    full tick -> decision -> tick_once(spawn_allowance=...)
    while monotonic() < next_full:
        sleep to the next check (settle)
        re-read LIVE settings (mtime-cached config)
        if mode != enforce or paused or nothing_admissible_latched: continue
        decision = controller.decide(now)
        if decision.allowance and host_has_admissible_work():
            tick_once(admission_only=True, spawn_allowance=...)
            record(decision.now, spawned_total)

Every mode uses the same helper; ``off`` only re-reads settings and never
decides or spawns, so a mode switch applies within one settle period.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import gateway.kanban_watchers as kwatch
from hermes_cli import kanban_admission as ka


class FakeClock:
    """Injectable monotonic clock + sleep recorder."""

    def __init__(self, start=100.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        seconds = max(0.0, seconds)
        self.slept.append(seconds)
        self.now += seconds


def _settings_block(**over):
    base = dict(
        mode="off", step=2, settle_seconds=5.0, backoff_cooldown_seconds=10.0,
        min_running=2, cpu_psi_hold=30.0, cpu_psi_backoff=60.0,
        headroom_min_gib=8, headroom_worker_multiple=4,
    )
    base.update(over)
    return base


def _signals(cpu=1.0):
    return ka.HostSignals(
        cpu_psi=cpu, mem_psi=0.0, mem_avail_bytes=40 * 1024**3,
        mem_total_bytes=62 * 1024**3, mem_level="ok", sampled_at=None)


class Harness:
    """A runner mixin instance with every dependency of _inter_tick_wait faked."""

    def __init__(self, monkeypatch, *, mode="off", settle=5.0, interval=60.0,
                 admissible=True, paused=False, cpu=1.0):
        self.clock = FakeClock(100.0)
        self.config = {
            "kanban": {
                "dispatch_interval_seconds": interval,
                "adaptive_admission": _settings_block(mode=mode, settle_seconds=settle),
            },
        }
        self.paused = paused
        self.admissible = admissible
        self.cpu = cpu
        self.spawn_calls: list[dict] = []
        self.terminated: list = []

        monkeypatch.setattr(kwatch.time, "monotonic", self.clock.monotonic)
        monkeypatch.setattr(kwatch.asyncio, "sleep", self.clock.sleep)
        # _live_kanban_config is a mixin method; patch it on the CLASS so the
        # instance created below sees the fake. It must return the KANBAN
        # block (the real method extracts it from the full config).
        monkeypatch.setattr(
            kwatch.GatewayKanbanWatchersMixin, "_live_kanban_config",
            lambda self: self.config.get("kanban") or {})
        monkeypatch.setattr(
            kwatch, "_kanban_dispatch_allowed", lambda: not self.paused)

        def fake_signals():
            return _signals(self.cpu)

        monkeypatch.setattr(ka, "read_host_signals", fake_signals)

        # host_running must be a stable number ≥ min_running so the liveness
        # override never fires in these loop tests (it's covered separately
        # in test_kanban_admission.py T8/T9).
        import hermes_cli.kanban_db_dispatch as kbd_mod

        monkeypatch.setattr(kbd_mod, "count_running_tasks_all_boards", lambda: 5)

        # The runner: a bare mixin instance whose _running we can flip, with
        # the harness config attached for the patched _live_kanban_config.
        runner = object.__new__(kwatch.GatewayKanbanWatchersMixin)
        runner._running = True
        runner.config = self.config
        self.runner = runner

        # The controller + dispatcher the loop drives.
        from hermes_cli import kanban_db_dispatch as kbd

        self.settings = ka.parse_admission_settings(self.config)
        self.controller = ka.AdmissionController(self.settings)

        harness = self

        class FakeDispatcher:
            settings = SimpleNamespace(max_in_progress=64)

            def tick_once(self, *, spawn_allowance=None, admission_reason=None,
                          admission_only=False):
                harness.spawn_calls.append({
                    "at": harness.clock.now,
                    "allowance": spawn_allowance,
                    "admission_only": admission_only,
                    "reason": admission_reason,
                })
                n = min(spawn_allowance, 2) if spawn_allowance else 0
                return [("default", SimpleNamespace(spawned=[("t", "a", "w")] * n))]

            def host_has_admissible_work(self):
                return harness.admissible

            def board_fingerprints(self):
                return {"default": ("/db/default", 1, 1)}

        self.dispatcher = FakeDispatcher()

        # Stop the loop after the fake clock passes a deadline: the helper
        # loops until next_full, so flip _running off from inside sleep.
        # NOTE: kwatch.asyncio.sleep is the seam the loop calls; rebind BOTH
        # it and the clock so test-local clamps compose with this one.
        harness = self
        orig_sleep = self.clock.sleep
        self.deadline = 160.0

        async def clamped_sleep(seconds):
            await orig_sleep(seconds)
            if harness.clock.now >= harness.deadline:
                harness.runner._running = False

        self.clock.sleep = clamped_sleep
        kwatch.asyncio.sleep = clamped_sleep

    async def run(self):
        await self.runner._inter_tick_wait(
            controller=self.controller,
            dispatcher=self.dispatcher,
            interval=60.0,
        )


# ---------------------------------------------------------------------------
# T27: pacing cadence under unbounded backlog
# ---------------------------------------------------------------------------


def test_t27_enforce_paces_between_full_ticks(monkeypatch):
    """Unbounded backlog, instant spawns: 10-12 sub-passes per 60 fake seconds
    and never two grants within 4.5 fake seconds."""
    h = Harness(monkeypatch, mode="enforce", settle=5.0)
    asyncio.run(h.run())

    sub_passes = [c for c in h.spawn_calls if c["admission_only"]]
    assert 10 <= len(sub_passes) <= 12, [c["at"] for c in sub_passes]
    grants = [c["at"] for c in sub_passes if c["allowance"]]
    assert len(grants) == len(sub_passes), "every sub-pass had a positive allowance"
    for a, b in zip(grants, grants[1:]):
        assert b - a >= 4.5, (a, b)


def test_t27_first_sub_pass_at_settle_after_full_tick(monkeypatch):
    """The first check lands one settle after the full tick anchored at t_full."""
    h = Harness(monkeypatch, mode="enforce", settle=5.0)
    asyncio.run(h.run())
    assert h.spawn_calls, "enforce with admissible work must sub-pass"
    assert h.spawn_calls[0]["at"] == 105.0


def test_t27_no_sub_pass_when_probe_false(monkeypatch):
    h = Harness(monkeypatch, mode="enforce", admissible=False)
    asyncio.run(h.run())
    assert h.spawn_calls == []


def test_t27_no_sub_pass_when_paused(monkeypatch):
    h = Harness(monkeypatch, mode="enforce", paused=True)
    asyncio.run(h.run())
    assert h.spawn_calls == []


def test_t27_no_sub_pass_in_shadow_or_off(monkeypatch):
    for mode in ("shadow", "off"):
        h = Harness(monkeypatch, mode=mode)
        asyncio.run(h.run())
        assert h.spawn_calls == [], mode


def test_t27_held_cpu_still_spawns_nothing(monkeypatch):
    """A RED (cpu_psi over backoff) yields zero-allowance sub-passes only:
    the tick is attempted only when the allowance is positive."""
    h = Harness(monkeypatch, mode="enforce", cpu=90.0)
    asyncio.run(h.run())
    assert h.spawn_calls == []


def test_t27_mode_switch_takes_effect_within_settle(monkeypatch):
    """off -> enforce via a config edit applies within <= 5 fake seconds."""
    h = Harness(monkeypatch, mode="off")
    orig_sleep = h.clock.sleep

    async def clamped(seconds):
        await orig_sleep(seconds)
        if h.clock.now >= 103.0 and h.config["kanban"]["adaptive_admission"]["mode"] == "off":
            h.config["kanban"]["adaptive_admission"]["mode"] = "enforce"

    # Rebind the seam the loop actually calls, composing with the harness
    # deadline clamp (which still owns flipping _running at 160).
    h.clock.sleep = clamped
    kwatch.asyncio.sleep = clamped
    asyncio.run(h.run())

    assert h.spawn_calls, "enforce must begin spawning after the switch"
    assert h.spawn_calls[0]["at"] <= 110.0, "must take effect within one settle + slack"


# ---------------------------------------------------------------------------
# T20a: #117755 absent — off is live, ceiling is the boot snapshot
# ---------------------------------------------------------------------------


def test_t20a_mode_switch_to_off_stops_sub_passes_live(monkeypatch):
    """enforce -> off with a config mtime bump stops sub-passes and the
    allowance at the next check (<= settle)."""
    h = Harness(monkeypatch, mode="enforce")
    orig_sleep = h.clock.sleep
    switched = {"at": None}

    async def clamped(seconds):
        await orig_sleep(seconds)
        if h.clock.now >= 103.0 and switched["at"] is None:
            switched["at"] = h.clock.now
            h.config["kanban"]["adaptive_admission"]["mode"] = "off"

    # Rebind the seam the loop actually calls (deadline clamp stays armed).
    h.clock.sleep = clamped
    kwatch.asyncio.sleep = clamped
    asyncio.run(h.run())

    # The switch lands mid-sleep at 103; the check already past its mode gate
    # at 100 may still sub-pass (within one settle — that IS the contract).
    # From the first check that READ the new mode onward: nothing.
    first_post_switch_check = switched["at"] + 5.0  # next check reads >= 105
    after = [c for c in h.spawn_calls if c["at"] >= first_post_switch_check]
    assert after == [], "no sub-pass after the first post-switch check"
    assert h.spawn_calls, "the enforce period must have sub-passed"
    assert h.spawn_calls[-1]["at"] <= 105.0


def test_t20a_ceiling_restart_pending_reported(monkeypatch):
    """Status fields show ceiling_restart_pending=true when the boot ceiling
    and the live configured value differ (#117755 absent)."""
    ctl = ka.AdmissionController(ka.parse_admission_settings({
        "kanban": {"adaptive_admission": _settings_block(mode="shadow")}}))
    d = ctl.decide(
        now=100.0, signals=_signals(),
        settings=ctl.settings, ceiling=64, host_running=5,
    )
    fields = ka.status_fields(d, ceiling_configured=32)
    assert fields["ceiling"] == 64
    assert fields["ceiling_configured"] == 32
    assert fields["ceiling_restart_pending"] is True


def test_t20a_ceiling_restart_pending_false_when_equal():
    ctl = ka.AdmissionController(ka.parse_admission_settings({
        "kanban": {"adaptive_admission": _settings_block(mode="shadow")}}))
    d = ctl.decide(
        now=100.0, signals=_signals(),
        settings=ctl.settings, ceiling=64, host_running=5,
    )
    fields = ka.status_fields(d, ceiling_configured=64)
    assert fields["ceiling_restart_pending"] is False


# ---------------------------------------------------------------------------
# T21: held admission never signals anything
# ---------------------------------------------------------------------------


def test_t21_allowance_zero_never_terminates(monkeypatch):
    """Allowance 0 with running workers: nothing spawned, nothing signalled."""
    h = Harness(monkeypatch, mode="enforce", cpu=90.0)  # RED -> allowance 0
    terminated: list = []
    h.runner.terminate_pids = terminated.append
    asyncio.run(h.run())
    assert h.spawn_calls == []
    assert terminated == []


# ---------------------------------------------------------------------------
# T30: nothing-admissible latch
# ---------------------------------------------------------------------------


def test_t30_latch_blocks_until_fingerprint_change(monkeypatch):
    """A sub-pass with allowance > 0 that spawns 0 (all rows guarded) stops
    further sub-passes; a board fingerprint change re-enables them."""
    h = Harness(monkeypatch, mode="enforce")
    orig_sleep = h.clock.sleep
    fp_version = {"v": 0}

    def fingerprints():
        return {"default": ("/db/default", fp_version["v"], 1)}

    h.dispatcher.board_fingerprints = fingerprints
    spawn_calls = h.spawn_calls

    def tick_once(*, spawn_allowance=None, admission_reason=None, admission_only=False):
        spawn_calls.append({
            "at": h.clock.now, "allowance": spawn_allowance,
            "admission_only": admission_only,
        })
        # Every attempt yields zero (guarded backlog) UNTIL the fingerprint
        # changes at t=112 (a new ready row landed).
        n = 0 if fp_version["v"] == 0 else min(spawn_allowance or 0, 2)
        return [("default", SimpleNamespace(spawned=[("t", "a", "w")] * n))]

    h.dispatcher.tick_once = tick_once

    async def clamped(seconds):
        await orig_sleep(seconds)
        if h.clock.now >= 112.0 and fp_version["v"] == 0:
            fp_version["v"] = 1

    # Rebind the seam the loop actually calls (deadline clamp stays armed).
    h.clock.sleep = clamped
    kwatch.asyncio.sleep = clamped
    asyncio.run(h.run())

    sub_passes = [c for c in spawn_calls if c["admission_only"]]
    before_change = [c for c in sub_passes if c["at"] < 112.0]
    after_change = [c for c in sub_passes if c["at"] >= 112.0]
    assert len(before_change) == 1, [c["at"] for c in sub_passes]
    assert after_change, "a fingerprint change must re-enable sub-passes"
    grants = [c["at"] for c in after_change]
    for a, b in zip(grants, grants[1:]):
        assert b - a >= 4.5


# ---------------------------------------------------------------------------
# T28: bad_ticks counts full ticks only; sub-pass spawns clear it
# ---------------------------------------------------------------------------


def test_t29_full_tick_shadow_applies_no_allowance(monkeypatch):
    """The full tick's decision: shadow computes (GREEN, reason) but returns
    NO allowance — today's full-tick semantics (T29)."""
    from hermes_cli import kanban_admission as ka

    monkeypatch.setattr(ka, "read_host_signals", lambda: _signals())
    import hermes_cli.kanban_db_dispatch as kbd_mod

    monkeypatch.setattr(kbd_mod, "count_running_tasks_all_boards", lambda: 5)

    for mode, expect_allowance in (("shadow", None), ("enforce", 2), ("off", None)):
        ctl = ka.AdmissionController(ka.parse_admission_settings(
            {"adaptive_admission": _settings_block(mode=mode)}))
        allowance, reason, decision = asyncio.run(kwatch._full_tick_admission(
            ctl,
            max_in_progress=64,
            live_kanban_config={"adaptive_admission": _settings_block(mode=mode)},
            clock=lambda: 100.0,
        ))
        if mode == "off":
            assert (allowance, reason, decision) == (None, None, None)
        else:
            assert decision is not None and decision.level == "GREEN"
            assert allowance == expect_allowance, mode
            assert decision.allowance == 2, f"{mode} decision still computed"


def test_t28_bad_tick_update_ignores_sub_pass_spawns():
    """A full tick with ready work and 0 full-tick spawns doesn't increment
    bad_ticks when a sub-pass spawned since the previous full tick."""
    # Sub-pass spawned -> not bad.
    assert kwatch._bad_tick_update(True, False, 2, 3) == 0
    # Nothing spawned at all -> increments.
    assert kwatch._bad_tick_update(True, False, 0, 3) == 4
    # No ready work -> reset.
    assert kwatch._bad_tick_update(False, False, 0, 3) == 0
    # Full tick itself spawned -> not bad.
    assert kwatch._bad_tick_update(True, True, 0, 3) == 0
