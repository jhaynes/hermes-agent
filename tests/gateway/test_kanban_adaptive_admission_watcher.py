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
        if h.clock.now >= 112.0 and switched["at"] is None:
            switched["at"] = h.clock.now
            h.config["kanban"]["adaptive_admission"]["mode"] = "off"

    # Rebind the seam the loop actually calls (deadline clamp stays armed).
    h.clock.sleep = clamped
    kwatch.asyncio.sleep = clamped
    asyncio.run(h.run())

    # The enforce period ran (checks at 105 and 110 spawned before the
    # edit landed mid-sleep at 112).
    before = [c for c in h.spawn_calls if c["at"] < 113.0]
    assert len(before) >= 2, [c["at"] for c in h.spawn_calls]
    # The check at 115 reads the edited mode (fresh read at each check,
    # §5.2): no sub-pass from the first post-edit check onward.
    after = [c for c in h.spawn_calls if c["at"] >= 115.0]
    assert after == [], "no sub-pass after the first post-switch check"
    assert h.spawn_calls[-1]["at"] <= 110.0


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


# ---------------------------------------------------------------------------
# Production-wiring tests (R1): the REAL _kanban_dispatcher_watcher, with the
# DB layer faked exactly as the shipped code drives it. The T27 Harness above
# always injects a pre-built controller, so it can never see the boot-mode
# wiring — these tests drive the real boot path (reviewer probes P3/P4A/Q1
# reproduced a dispatcher-killing AttributeError there).
# ---------------------------------------------------------------------------


class _ProdWiringSandbox:
    """Fake every production dependency of _kanban_dispatcher_watcher.

    Mirrors the shipped call graph: boot -> _resolve_dispatcher_settings ->
    zombie reap -> pause gate -> auto-decompose -> full-tick admission ->
    tick_once -> ready_nonempty -> bad-tick telemetry -> _inter_tick_wait.
    The board DB layer is replaced at exactly the points it touches SQLite
    (tick_once_for_board, ready_nonempty); everything else — settings
    resolution, the controller wiring, admission, pacing, telemetry — is
    the shipped code under test.
    """

    def __init__(self, monkeypatch, tmp_path, *, boot_mode, live_mode,
                 full_tick_spawns=0, sub_pass_spawns=2, admissible=True,
                 interval=60.0, ceiling=64, intervals=3,
                 full_tick_spawn_delay=0.0, live_settle=None):
        import gateway.kanban_watchers_dispatcher as kwd_mod
        import hermes_cli.kanban_db as kb_mod
        import hermes_cli.kanban_db_dispatch as kbd_mod

        self.clock_now = 1000.0
        self.full_tick_spawns = full_tick_spawns
        self.sub_pass_spawns = sub_pass_spawns
        self.admissible = admissible
        self.full_tick_spawn_delay = full_tick_spawn_delay
        # live_settle: (flip_at, value) — flip the LIVE settle_seconds once
        # the fake clock passes flip_at (mid-interval tuning edit).
        self.live_settle = live_settle
        self.calls: list[dict] = []
        self.full_ticks = 0
        self.sub_passes = 0
        # Long enough for the 6-tick "stuck" window to be able to fire when
        # the caller wants to assert on it (intervals=8); short (3) otherwise.
        self.deadline = 1000.0 + 5 + interval * intervals
        home = tmp_path / "prodtest-home"
        home.mkdir()

        self.boot_cfg = {
            "dispatch_interval_seconds": interval,
            "max_in_progress": ceiling,
            "adaptive_admission": {"mode": boot_mode},
        }
        self.live_block = {"mode": live_mode}
        self.status_path = home / "kanban" / "admission_status.json"

        monkeypatch.setattr(kwatch.time, "monotonic", lambda: self.clock_now)
        monkeypatch.setattr(kwatch.time, "time", lambda: self.clock_now)
        monkeypatch.setattr(kwatch, "_kanban_dispatch_allowed", lambda: True)
        monkeypatch.setattr(
            kwatch, "_resolve_auto_decompose_settings", lambda _l: (False, 0))
        monkeypatch.setattr(ka, "read_host_signals", lambda: _signals())
        monkeypatch.setattr(
            kbd_mod, "count_running_tasks_all_boards", lambda: 5)
        monkeypatch.setattr(kbd_mod, "reap_worker_zombies", lambda: [])

        self._kb = SimpleNamespace(
            kanban_home=lambda: home,
            DEFAULT_BOARD="default",
            DEFAULT_FAILURE_LIMIT=3,
            list_boards=lambda **_kw: [{"slug": "default"}],
            read_board_metadata=lambda slug: {"slug": slug},
        )

        def fake_tick_once_for_board(dispatcher_self, slug, **kwargs):
            sub_pass = kwargs.get("admission_only")
            spawned = self.sub_pass_spawns if sub_pass else self.full_tick_spawns
            self.calls.append({
                "at": self.clock_now,
                "allowance": kwargs.get("spawn_allowance"),
                "admission_only": bool(sub_pass),
            })
            if sub_pass:
                self.sub_passes += 1
            else:
                self.full_ticks += 1
                if self.full_tick_spawn_delay:
                    # A slow full tick: the spawn completes this many fake
                    # seconds AFTER its decision (the loop's entry anchor and
                    # the pacing stamp differ by exactly this).
                    self.clock_now += self.full_tick_spawn_delay
            # A real DispatchResult shape: the watcher's telemetry reads
            # .spawned/.respawn_guarded/.skipped_per_profile_capped off it.
            result = kb_mod.DispatchResult()
            result.spawned = [("t", "a", "w")] * spawned
            return result

        monkeypatch.setattr(
            kwd_mod._KanbanDispatcher, "tick_once_for_board",
            fake_tick_once_for_board)
        monkeypatch.setattr(
            kwd_mod._KanbanDispatcher, "ready_nonempty",
            lambda dispatcher_self: self.admissible)

        sandbox = self

        async def fake_sleep(seconds):
            seconds = max(0.0, seconds)
            sandbox.clock_now += seconds
            if (sandbox.live_settle is not None
                    and sandbox.clock_now >= sandbox.live_settle[0]):
                sandbox.live_block["settle_seconds"] = sandbox.live_settle[1]
            if sandbox.clock_now >= sandbox.deadline:
                sandbox.runner._running = False

        monkeypatch.setattr(kwatch.asyncio, "sleep", fake_sleep)

        runner = object.__new__(kwatch.GatewayKanbanWatchersMixin)
        runner._running = True  # type: ignore[method-assign]
        runner._kanban_dispatcher_lock_handle = None
        runner.config = {"kanban": self.boot_cfg}
        runner._kanban_dispatcher_boot = lambda: (None, self._kb, self.boot_cfg)
        runner._live_kanban_config = lambda: {
            "dispatch_interval_seconds": interval,
            "max_in_progress": ceiling,
            "adaptive_admission": self.live_block,
        }
        self.runner = runner

    @property
    def full_tick_calls(self) -> list[dict]:
        return [c for c in self.calls if not c["admission_only"]]

    @property
    def sub_pass_calls(self) -> list[dict]:
        return [c for c in self.calls if c["admission_only"]]

    async def run_watcher(self):
        await self.runner._kanban_dispatcher_watcher()

    def read_status(self) -> dict:
        import json

        return json.loads(self.status_path.read_text())


def test_prod_wiring_boot_off_live_enforce_spawns_without_crash(
    monkeypatch, tmp_path,
):
    """Boot mode=off (the shipped default) then a live switch to enforce:
    the controller must exist (or be built lazily) so enforce takes effect
    within one settle — NOT by crashing and relying on a supervised restart
    (plan §5.2 MoA B3, §6 liveness; R1: every lane, probes P1/P3/P4A/Q1)."""
    box = _ProdWiringSandbox(
        monkeypatch, tmp_path, boot_mode="off", live_mode="enforce")
    asyncio.run(box.run_watcher())  # must not raise

    assert box.sub_passes > 0, "enforce must begin sub-passing after the switch"
    assert box.full_ticks >= 2, "full ticks must keep running throughout"


def test_prod_wiring_boot_off_live_shadow_decides_between_full_ticks(
    monkeypatch, tmp_path,
):
    """Boot mode=off then a live switch to shadow: shadow computes every
    decision on the sub-pass schedule (§5.1) — no sub-pass spawns, but the
    status file advances between full ticks (R1 quality Q3 / arch A5)."""
    box = _ProdWiringSandbox(
        monkeypatch, tmp_path, boot_mode="off", live_mode="shadow")
    asyncio.run(box.run_watcher())

    assert box.sub_passes == 0, "shadow never runs sub-pass spawns"
    status = box.read_status()
    assert status.get("mode") == "shadow"
    counters = status.get("shadow_counters") or {}
    # 3 fake intervals x ~11 checks/interval: shadow must have decided far
    # more often than once per full tick (the sub-pass cadence contract).
    assert counters.get("decisions", 0) > box.full_ticks + 1, (
        f"shadow decided only {counters.get('decisions')} times over "
        f"{box.full_ticks} full ticks — sub-pass-schedule decisions missing")


def test_prod_wiring_sub_pass_spawns_clear_bad_ticks(monkeypatch, tmp_path):
    """A paced host whose sub-passes do the spawning must not trip the
    6-tick "stuck" warning: full ticks spawn 0 with ready work pending,
    every sub-pass spawns (§5.2 MoA I4, T28 — R1 tests F2 / arch A2,
    probe P4: 88 spawns, 3 'stuck' warnings)."""
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    # gateway/kanban_watchers.py logs through the shared `gateway.run` logger.
    log = logging.getLogger("gateway.run")
    old_level, old_propagate = log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.propagate = False
    try:
        box = _ProdWiringSandbox(
            monkeypatch, tmp_path, boot_mode="enforce", live_mode="enforce",
            full_tick_spawns=0, sub_pass_spawns=2, admissible=True,
            intervals=8)
        asyncio.run(box.run_watcher())
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        log.propagate = old_propagate

    stuck = [m for m in records if "stuck" in m]
    assert not stuck, f"healthy paced host logged 'stuck': {stuck[:1]}"
    assert box.sub_passes > 0 and box.full_ticks >= 2


def test_prod_wiring_full_tick_applies_enforce_allowance(monkeypatch, tmp_path):
    """W2 (R1 tests F3): the full tick in ENFORCE must pass the controller's
    allowance to ``tick_once`` — not None. A None allowance would revert the
    full tick to today's uncapped burst (plan §5.2: the full tick is the
    interval's first decision point; enforce applies it)."""
    box = _ProdWiringSandbox(
        monkeypatch, tmp_path, boot_mode="enforce", live_mode="enforce",
        full_tick_spawns=2, sub_pass_spawns=2)
    asyncio.run(box.run_watcher())

    assert box.full_ticks >= 2, "the sandbox must have run full ticks"
    for call in box.full_tick_calls:
        assert call["allowance"] == 2, (
            f"full tick passed spawn_allowance={call['allowance']!r} — "
            "enforce must apply the step at the full tick (W2)")


def test_prod_wiring_full_tick_grant_paces_first_sub_pass(monkeypatch, tmp_path):
    """W1 (R1 tests F3): the full tick's grant must start the pacing window
    (§5.2 "a grant used at the full tick paces the first sub-pass exactly
    like a sub-pass grant"). Observable by lengthening the LIVE settle mid-
    interval: the check the schedule already committed to (t=1010, one boot
    settle after the full tick) must be held by pacing — it is 5s after the
    full-tick decision, less than the new 7s settle."""
    box = _ProdWiringSandbox(
        monkeypatch, tmp_path, boot_mode="enforce", live_mode="enforce",
        full_tick_spawns=2, sub_pass_spawns=2, intervals=2,
        live_settle=(1006.0, 7.0))
    asyncio.run(box.run_watcher())

    assert box.full_tick_spawns, "the full tick must have spawned (record n>=1)"
    sub_times = [c["at"] for c in box.sub_pass_calls]
    assert sub_times, "sub-passes must run while enforce is on"
    # The full-tick decision was at 1005 (boot settle 5 seeded the first
    # check at 1010). The settle edit to 7 landed at 1006, so the check at
    # 1010 — only 5s after the full-tick grant — must be PACED (held):
    # 5 < 7 - PACING_SLACK. A dropped full-tick record (W1) grants there.
    assert 1010.0 not in sub_times, (
        f"the check one boot-settle after the full tick granted early: "
        f"{sub_times[:3]} — the full-tick grant must pace it (W1)")
    # The next grid point (1017, one NEW settle after 1010) is 12s after the
    # full-tick decision: pacing satisfied, it must have granted.
    assert min(sub_times) >= 1017.0, sub_times[:3]


def test_prod_wiring_full_tick_stamp_uses_decision_time(monkeypatch, tmp_path):
    """A3 (R1 arch A3 / tests F3): the full tick's ``record`` must stamp the
    DECISION time, never spawn completion (§5.2 MoA B2). The full tick's
    spawn takes 2 fake seconds (decision at 1005, completion at 1007); the
    live settle is lengthened to 7 at 1008. The first check (1012) is then
    7s after the DECISION (pacing satisfied at 7 >= 7 - 0.5) but only 5s
    after the COMPLETION — a completion-stamped window would hold it."""
    box = _ProdWiringSandbox(
        monkeypatch, tmp_path, boot_mode="enforce", live_mode="enforce",
        full_tick_spawns=2, sub_pass_spawns=2, intervals=2,
        full_tick_spawn_delay=2.0, live_settle=(1008.0, 7.0))
    asyncio.run(box.run_watcher())

    sub_times = [c["at"] for c in box.sub_pass_calls]
    assert 1012.0 in sub_times, (
        f"the first sub-pass must grant at the decision-anchored window "
        f"(1012); got {sub_times[:3]} — the full-tick stamp is not the "
        "decision time (A3)")


def test_prod_wiring_status_reports_ceiling_restart_pending(
    monkeypatch, tmp_path,
):
    """T20a at loop level (R1 tests F4): with #117755 absent, the status
    file written by the REAL watcher reports ``ceiling`` from the dispatcher
    boot snapshot and ``ceiling_configured`` from the live config, flagging
    ``ceiling_restart_pending: true`` when they differ (§7.1)."""
    box = _ProdWiringSandbox(
        monkeypatch, tmp_path, boot_mode="enforce", live_mode="enforce",
        full_tick_spawns=2, sub_pass_spawns=0, admissible=False)
    monkeypatch.setattr(ka, "live_configured_ceiling", lambda: 32)
    asyncio.run(box.run_watcher())

    status = box.read_status()
    assert status["ceiling"] == 64, "the enforced boot snapshot (b=64)"
    assert status["ceiling_configured"] == 32, "the live configured value"
    assert status["ceiling_restart_pending"] is True


def test_t27_settle_greater_than_interval_runs_no_sub_passes(monkeypatch):
    """settle_seconds > dispatch_interval_seconds: sub-passes never run and
    pacing applies across full ticks (§6; R1 tests F4 — no test at any
    layer). With settle=90 and interval=60, every check lands after the
    next full tick, so zero sub-passes."""
    h = Harness(monkeypatch, mode="enforce", settle=90.0, interval=60.0)
    asyncio.run(h.run())
    assert h.spawn_calls == []


def test_t20b_live_ceiling_reaches_decide_same_tick(monkeypatch):
    """T20b (skip-marked until #117755 lands on the base): a live
    ``settings.max_in_progress`` change reaches ``decide`` in the same tick
    once the dispatcher re-resolves its settings live (plan §10)."""
    pytest.skip("skip-marked until #117755 is on the base (plan §10)")


# ---------------------------------------------------------------------------
# T30 (R1 quality Q2): the latch fingerprint must see WAL commits
# ---------------------------------------------------------------------------


def test_board_fingerprint_sees_wal_sidecar_commits(tmp_path):
    """In WAL mode a committed row write moves the ``-wal`` sidecar long
    before the main DB is checkpointed, so the nothing-admissible latch's
    re-arm signal (``board_db_fingerprint``, §5.2) must cover the sidecar:
    a fingerprint over the main file alone never changes on exactly the
    writes that matter (new ready rows). Verified against a real WAL-mode
    SQLite file (R1 quality Q2 probe, reproduced 3 committed writes)."""
    import sqlite3
    import time as _time

    from gateway.kanban_watchers_dispatcher import (
        _KanbanDispatcher, _resolve_dispatcher_settings,
    )

    db = tmp_path / "default.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()

    kb = SimpleNamespace(
        kanban_db_path=lambda slug: db, DEFAULT_FAILURE_LIMIT=3)
    settings = _resolve_dispatcher_settings({}, kb)
    dispatcher = _KanbanDispatcher(kb, settings)

    before = dispatcher.board_db_fingerprint("default")
    for i in range(3):
        conn.execute("INSERT INTO t VALUES (?)", (i,))
        conn.commit()
        _time.sleep(0.02)  # distinct mtime_ns per commit
    after = dispatcher.board_db_fingerprint("default")

    assert before != after, (
        "board fingerprint blind to WAL commits — the nothing-admissible "
        "latch never re-arms on new ready rows (Q2)")

    # A write that touches the main file (checkpoint) must also change it.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    assert after != dispatcher.board_db_fingerprint("default")

    # Boards without a sidecar (DELETE journal mode) stat stably: two
    # consecutive fingerprints with no writes are identical.
    conn.close()
    stable_a = dispatcher.board_db_fingerprint("default")
    stable_b = dispatcher.board_db_fingerprint("default")
    assert stable_a == stable_b, "fingerprint must be stable across reads"


def test_board_fingerprint_missing_db_is_stable(tmp_path):
    """A missing/unreadable board DB fingerprints as (path, Nones) — stable,
    never raising, and never spuriously re-arming the latch."""
    from gateway.kanban_watchers_dispatcher import (
        _KanbanDispatcher, _resolve_dispatcher_settings,
    )

    db = tmp_path / "absent.db"
    kb = SimpleNamespace(
        kanban_db_path=lambda slug: db, DEFAULT_FAILURE_LIMIT=3)
    settings = _resolve_dispatcher_settings({}, kb)
    dispatcher = _KanbanDispatcher(kb, settings)

    first = dispatcher.board_db_fingerprint("default")
    second = dispatcher.board_db_fingerprint("default")
    assert first == second
    assert first[0].endswith("absent.db")
    assert first[1] == (None, None)
    assert first[2] == (None, None)
