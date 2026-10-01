# PLAN rev2: adaptive Kanban worker concurrency on tower (seconds-scale pacing)

Card: t_1c59a0b2 (plan only; no code, config, service, push). Author: builder.
Supersedes: `2026-09-25-kanban-adaptive-concurrency.md` (t_d7e089f1,
PLAN_SHA256=220d527682536b4f25a8c17e37222b07f8b2d953e8deee80f284e5cff258e483, verified before
this revision). This file is self-contained: the BUILD card needs only this file.
Base inspected: NousResearch/hermes-agent origin/main d0288be5b3 (worktree fast-forwarded from
1e21fe8624; no local changes).
Status: rev2 v2, reconciled after MoA review (section 14). Awaiting Justin's approval, including the
one ceiling decision in section 1.1, before any BUILD card is created.

## 0. Contract (verbatim scope, provenance)

Original (t_d7e089f1), Justin 2026-09-25 ~23:15 PDT: "We should be bumping the slots up on tower as
we do work until we get to memory or cpu pressure." The plan must settle: (1) control law, (2) config
shape and exposure, (3) interaction with the per-profile cap, cross-board host budget and provider
rate limits, (4) landing and tower rollout, (5) tests and mutants, (6) exact serial test commands.
Non-goals: killing running workers; changing admission when the feature is off; installing anything
on tower (a later approved card does that).

Binding clarification (comment on t_d7e089f1, `default`, 2026-09-25 ~23:47 PDT): rate limits follow
each run's RESOLVED provider (task override, else profile default), not the profile.
`max_in_progress_per_profile` stays a static operator cap. The per-provider budget is upstream issue
#123654 (separate card). Rev2 does not implement it and must stay compatible (it plugs in as one more
downstream clamp, section 7).

Amendment (t_1c59a0b2), Justin 2026-09-26: rev1 pacing is too slow. `settle_seconds` 120 -> ~5 s,
`backoff_cooldown_seconds` 300 -> ~10 s. "Why wait so long? Tell me otherwise if it should be that
slow or slower than my gut reaction." Start from his numbers and deviate only with recorded
evidence. Rev2 must resolve: tick cadence, PSI window, worker ramp time, a leading memory headroom
guard, the cooldown's value, step size, and behavior when #117755 has not landed.

What this design is (stated plainly, MoA B1): pressure-gated, paced ADMISSION under an operator
ceiling. It grows the running count toward the ceiling as fast as pacing allows while the host shows
no CPU contention and has memory headroom. It never kills or throttles running workers. It therefore
bounds memory at admission time only. A worker admitted while idle can later start a 4 GiB test
suite, and only the ceiling, the per-worker MemoryMax and the kernel bound that later growth.

## 1. Justin's gut vs. evidence

| Key | Justin | Chosen | Why (evidence in section 2) |
|---|---|---|---|
| `settle_seconds` | ~5 s | **5 s** | New workers reach 70 % of steady anon RSS by 5 s and 98 % by 30 s. They use 4.2 CPU-s in their first 5 s, then about 0.02 cores. What's still ramping after 5 s is ~70 MiB/worker, noise next to a 16 GiB headroom floor. |
| `backoff_cooldown_seconds` | ~10 s | **10 s** (a small debounce) | avg10 decays 40 -> 20 in ~7 s by itself, so after a PSI RED the cooldown adds ~3 s. It is the only hysteresis for the instantaneous MemAvailable tiers, which can bounce as suite phases allocate and free. |
| Dispatch cadence | (5 s implied) | **Admission-only sub-pass every `settle_seconds`; full tick stays 60 s** | A full tick is cheap (median 0.14 s), but a 5 s full tick multiplies `respawn_guarded` events (~170/day today), auto-decompose LLM calls, tick hooks and the 6-tick "stuck" window by 12. The sub-pass only claims and spawns. Its reads take 2-10 ms. |
| PSI window | (avg60 in rev1) | **avg10** | It matches the 5 s cadence. In a measured suite spike, avg60 was still 9.8 when avg10 had already fallen to 3.9 at suite exit. avg60 would re-impose minute-scale holds. |
| `step` | (2 in rev1) | **2** | At most 24 workers/min. The startup transient is 2 x 0.85 cores and 2 x ~170 MiB at a time, invisible to PSI. The ceiling and headroom floor bind well before the rate does. |
| CPU thresholds | (hold 20 / backoff 40 in rev1) | **hold 30 / backoff 60** (avg10) | One full `tests/hermes_cli/` suite on 15 of 20 cores peaked at 23.2 and was at >= 20 for only 4 % of its run. Holding at 20 would freeze LLM-bound admissions (0.02 cores each) for no benefit. 30 leaves a single suite unheld, and two concurrent suites are expected to cross it (checked in shadow mode). |
| Memory PSI | (hold 5 / backoff 15 in rev1) | **not a control signal** (recorded only) | Host PSI is hierarchical and includes reclaim inside each worker's own 4 GiB MemoryMax, where 5 scopes had limit hits. One suite drove host memory avg10 to 5.18 with 39 GiB free. Real host exhaustion is caught earlier by the headroom floor and the MemAvailable tiers. |
| Headroom floor | (new, card item 4) | **max(8 GiB, 4 x per-worker MemoryMax), clamped to MemTotal/2 = 16 GiB on tower** | Suites reach the 4 GiB per-worker cap (4055 MiB observed; memory.peak p95 = 4096 MiB), and 4 of 19 live workers held more than 2 GiB at once. A p95 of current RSS would size for idle workers (~240 MiB anon), which is exactly the case that doesn't bite. |

Answer to "should it be slower than my gut?": no. Nothing measured argues for slower pacing.
Pacing is not the safety mechanism, because a worker's dangerous load (test suites, npm/vitest)
starts minutes after spawn, which no plausible settle window can observe. Safety comes from the
leading headroom floor, the ceiling, the per-worker 4 GiB MemoryMax and the MemAvailable tiers.
Pacing only has to cover the ~5-10 s startup transient.

### 1.1 Decision required at approval: what bounds later growth (MoA B1)

Admission control cannot bound memory growth AFTER admission. The only aggregate bound is
`kanban.max_in_progress`. From the 00:15 snapshot (19 workers: anon median 243 MiB; 4 heavy scopes
at 1.6-2.1 GiB anon and up to 4 GiB charged), an expected ~1 GiB per worker at a ~21 % heavy
fraction means a ceiling of 64 could commit ~66 GiB if the heavy fraction held at that size. That
exceeds tower's 62.5 GiB, backstopped by 48 GiB swap and each scope's 4 GiB MemoryMax. Tower ran
18-32 workers all night with 40+ GiB available, so the snapshot fraction may not scale with count:
heavy workers are mostly builders, which `max_in_progress_per_profile` (16) caps.

Justin picks one when approving. The BUILD is identical under all three; only the install card's
ceiling differs:
- (a) Commitment ceiling: the install card sets `max_in_progress` from shadow-mode data, roughly
  `MemAvailable_budget / (idle + f_heavy x (worker_bound - idle))`. Adaptive then mostly adds fast
  pacing and holds below that ceiling.
- (b) Pressure ceiling (RECOMMENDED; matches "bump until pressure"): keep a high ceiling (64) and
  explicitly accept per-scope MemoryMax, reclaim, swap and the headroom floor (which stops
  admission as soon as MemAvailable < 16 GiB) as the backstop for post-admission growth. Shadow mode
  (section 8) first measures how close this comes to the MemAvailable elevated tier. If it ever
  crosses the tier, the install card falls back to (a).
- (c) Tighter per-worker bound for non-builder profiles. Out of scope here (it changes
  `tools/process_registry.py` policy). It would need its own card.

## 2. Evidence (observed on current main / tower)

### 2.1 Code facts (file:line on d0288be5b3)

- `hermes_cli/kanban_db_dispatch.py:1801-1823` `derive_default_max_in_progress` /
  `resolve_max_in_progress`: explicit `kanban.max_in_progress` wins, else
  `clamp(MemTotal/512MiB, 2, 8)`, else None (no cap) where MemTotal is unreadable.
- `:1826` `configured_max_in_progress()` reads config via `load_config_readonly` (mtime-cached,
  with a last-known-good fallback on parse failure: `hermes_cli/config.py:2337-2347`).
- `:1846` `count_running_tasks`, `:1863` `count_running_tasks_other_boards(board)`: host budget across
  boards; each fails open (0) per board.
- `:1730-1757` `_has_spawnable` / `has_spawnable_ready` / `has_spawnable_review`: "any assigned,
  unclaimed row whose profile exists". They do NOT apply respawn guards or per-profile caps.
- `:1900` `_memory_pressure_level` -> `gateway/memory_status.py:50` `classify_pressure`
  (critical: MemAvailable < 64 MiB or < 5 %; elevated: < 128 MiB or < 15 %). Unknown means no
  restriction. `:1787` `_system_memory_sample` is the test seam (autouse-patched to `{}` in
  `tests/conftest.py:437`).
- `:1919-1975` `dispatch_once` wraps `_dispatch_once_locked` in the non-blocking per-board
  `_dispatch_tick_lock`, runs an interval-gated PASSIVE WAL checkpoint
  (`kanban_db_connect.py:224`) and fires `on_kanban_dispatch_tick` outside the lock.
- `:2136-2158` `_run_reclaim_phase`: zombie reap, terminal-worker reap, stale-claim release, orphan
  reconcile, stale/crash detection, max-runtime enforcement, then `recompute_ready` promotion.
- `:2161-2219` `_tick_spawn_budget`: `max_in_progress` is a HOST cap. After it comes the memory
  guard (critical -> 0 spawns, elevated -> <= 1 spawn per board call). Deferral only.
- `:2281-2382` `_dispatch_once_locked`: reclaim, budget, per-profile cap, review reservation
  (`ready_budget = spawn_budget - 1` when spawnable review work exists), then the ready and review
  lanes through `_dispatch_lane_task` (`:1991`). That function checks `check_respawn_guard` (`:1491`,
  SELECTs only) and, on a guard hit, WRITES a `respawn_guarded` event (`:2036-2038`). It also does the
  claim, workspace, spawn, `_set_worker_pid` and the `on_kanban_worker_spawned` hook.
  `DispatchResult` (`:100-160`) carries `spawned`, `respawn_guarded` and
  `skipped_per_profile_capped`.
- `recompute_ready` also runs on task completion (`kanban_db.py:2828`, plus `:1720`, `:3878`,
  `:3923`, `:3954`), so dependency-driven todo -> ready promotion doesn't wait for a dispatcher tick.
- `hermes_cli/kanban_db.py:231-236` `_TICK_ACTIVITY_FIELDS` and `:239` `_fire_dispatch_tick_hook`:
  one observer call per board per tick.
- `gateway/kanban_watchers.py:251-326` `_kanban_dispatcher_watcher`: per full tick it runs zombie
  reap, the pause gate, auto-decompose (an LLM call per triage task), `dispatcher.tick_once`,
  `ready_nonempty` and health telemetry (`_HEALTH_WINDOW = 6` ticks, `:37`), all via
  `_to_thread_process_service` (off the event loop), then `_sleep_between_ticks(interval)`
  (`:52-58`, 1 s slices).
- `gateway/kanban_watchers_dispatcher.py:35-118` `_DispatcherSettings` is resolved ONCE at boot.
  `interval` = `dispatch_interval_seconds` (default 60, floored at 1 s, `:50-55`). `:174-207`
  `tick_once_for_board` builds `dispatch_once` kwargs from `asdict(self.settings)`. `:140`
  `board_db_fingerprint` returns `(path, mtime_ns, size)`. Upstream #117734 (OPEN) tracks the
  boot-once read; fix PR #117755 is OPEN, `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY`
  (checked 2026-09-26 00:40 PDT).
- `:2909-2961` `run_daemon` (deprecated standalone loop) re-resolves `max_in_progress` every tick.
- `tools/process_registry.py:108-176`: every Kanban worker runs in `systemd-run --user --scope` with
  `MemoryMax = min(gateway cgroup memory.max, physical/2, 4 GiB)` (`_worker_memory_max_bytes`,
  `:113`). On tower that's 4 GiB (verified: scope `memory.max` = 4294967296). The ancestor slices
  (`user.slice`, `user-1000.slice`, `user@1000.service`, `app.slice`) have `memory.max` =
  `memory.high` = max (verified).
- The config rule that a new key in an existing section needs no `_config_version` bump is in
  `hermes_cli/AGENTS.md` ("Config system").

### 2.2 Tick cost (card item 1), measured 2026-09-26 00:10 PDT, 7 board DBs

- Full embedded tick wall time, from 71 consecutive tick pairs in `gateway.log` (the watcher sleeps
  60 s after each tick, so start-to-start drift beyond 60 s bounds the tick's own time): median
  0.136 s, p90 0.190 s, p99/max 0.428 s. Reap line -> per-board summary: 0.10-0.14 s, including ticks
  that spawned 10 workers (max 1.10 s).
- Admission read pass, all 7 boards read-only (running count, unclaimed ready/review rows,
  per-profile GROUP BY), 50 samples: median 2.0 ms, p90 8.3 ms, max 10.3 ms.
- `/proc/pressure/{cpu,memory}` + `/proc/meminfo`: median 0.018 ms, max 0.41 ms.
- SQLite contention: 0 `database is locked` lines in gateway/errors/agent logs. busy_timeout is
  120 s (`kanban_db_connect.py:49-73`).
- Side effects that scale with tick COUNT: `respawn_guarded` events in the last 24 h were
  triage-gate 144, smithers 18, hermes 3 (at 60 s ticks). Auto-decompose and the tick hook run
  every tick. The "stuck" warning fires after 6 bad ticks.
- Scripts: `receipts/2026-09-26-rev2-evidence/ticklog.py`, `admission_reads.py`.

### 2.3 Worker ramp (card item 3), measured 00:34-00:54 PDT, 8 freshly spawned scopes

Passive sampler (`ramp.py`) watching `app.slice/hermes-worker-*.scope` for new scopes:

| t after scope appears | 0 s | 2 s | 5 s | 10 s | 30 s | 60 s | 120 s |
|---|---|---|---|---|---|---|---|
| memory.current median MiB | 23 | 52 | 166 | 212 | 235 | 238 | 239 |
| anon median MiB | 22 | 50 | 162 | 208 | 231 | 233 | 233 |
| memory.current max MiB | 44 | 105 | 170 | 230 | 240 | 240 | 251 |
| cumulative CPU-s median | 0 | 2.0 | 4.2 | 4.7 | 5.5 | 5.9 | 7.0 |

Startup is ~0.85 cores for ~5 s (Python import and agent boot), then ~0.02 cores while idle and
LLM-bound. Workers' lifetime-average CPU is higher (median 3.3 %, p95 41 %; section 2.4) because of
later tool work. Memory reaches 70 % of steady state by 5 s, 89 % by 10 s and 98 % by 30 s. Host PSI
over the same 22 min (1315 one-second samples): cpu some avg10 p50 0.00 / p99 1.61 / max 2.24;
memory some avg10 max 0.32; MemAvailable 40.9-47.8 GiB. Report: `ramp_report.txt`.

### 2.4 The case that bites: builders running test suites (card item 4)

Snapshot of 19 live worker scopes at 00:15 PDT (`scopes.py`):
- memory.current median 336 MiB (includes page cache), p95 2437, max 2728. anon median 243, sum of
  current 14.7 GiB.
- memory.peak median 649 MiB, p95 4096, max 4096 (= MemoryMax). 4 of 19 held more than 2 GiB at once.
- 5 scopes had non-zero `memory.events max` (limit-reclaim hits, 331 to 171779).
- Lifetime-average CPU: median 3.3 %, p95 40.8 %, max 65.9 % of one core.

Controlled spike: `scripts/run_tests_parallel.py tests/hermes_cli/` in this worker's scope (the
runner auto-scales to ~15 of 20 cores), sampled every 0.5 s (`spike.py`, `spike_hermes_cli.json`):
- 332 s, 4715 CPU-s. The scope peaked at 4055 MiB (at MemoryMax). Host MemAvailable 43.97 -> min
  39.05 GiB.
- cpu some avg10 max 23.2. It was >= 20 for 4 % of the run and never >= 25 (`spike_replay.txt`).
  avg60 max 13.7.
- At suite exit, avg10 was 3.9 while avg60 was still 9.8, and 8.7 twelve seconds later.
- memory some avg10 max 5.18 while MemAvailable stayed at or above 39 GiB.

Kernel semantics: PSI is hierarchical, and `/proc/pressure/memory` is the root cgroup. A task
stalled in reclaim because its own scope hit `memory.max` counts toward the host too. Host memory
PSI can therefore rise when one worker thrashes against its own cap. A 90 s attribution sample on a
quiet host (`mempsi_attr_1.txt`) saw no stalls, so it neither confirms nor refutes attribution for
the spike. The design doesn't depend on it: memory PSI is not a control signal (section 5.4).

Not measured (explicitly open, answered by shadow mode): two or more concurrent suites; the
real-mix fraction of time any hold would bind.

### 2.5 Tower state (2026-09-26 00:00-00:55 PDT)

20 cores, MemTotal 62.5 GiB, swap 48.5 GiB mostly unused, 18-32 workers, load1 1.7-4.7,
MemAvailable 40.9-47.8 GiB. One gateway (pid 904250) holds `<kanban_home>/kanban/.dispatcher.lock`.

Correction to the card's facts: the gateway restarted at 00:05:39 and logged `max_in_progress=64`
and `max_in_progress_per_profile=16`, so the live boot snapshot is 64, not 32. The #117734 staleness
applies to edits after 00:05:39.

`scripts/run_tests.sh` in this Kanban worktree fails with "activate: no bootstrap Python found; run
setup-hermes.sh". The spike used an existing worktree venv's pytest through
`scripts/run_tests_parallel.py`. The BUILD must provision a venv first (section 9).

### 2.6 Prior art

`contrib/kanban-resource-controller/` (branch `ops/controller-configurable-thresholds` @57b334d8;
not on main) is a standalone out-of-process admission controller built for macOS (launchd), with a
Linux telemetry backend. It holds and recovers admission on load per core, MemAvailable (4/5 GiB),
swap-out growth and memory PSI some avg10 (warning 10), with a pacing dwell and at most one mutating
command per window. It requires `dispatch_in_gateway: false`, takes the dispatcher flock itself, and
requires Hermes `max_in_progress` to equal its own static host cap in `controller.json`.

## 3. Decision: where the logic lives (unchanged from rev1)

Chosen: extend the in-dispatcher admission guard (Footprint Ladder rung 1, extending existing code).
It is config-gated and OFF by default, lives in one new focused module
`hermes_cli/kanban_admission.py` called from the embedded dispatcher loop, and is upstream-shaped.

Rejected: running the resource controller on tower. It holds under static caps and doesn't grow. It
forces `dispatch_in_gateway: false` and a second dispatcher owner. It duplicates the host cap in
`controller.json` behind an equality preflight (two configurable places for one number). Its
one-command-per-window pacing is slower than even rev1. It is macOS-first, unreleased and
uninstalled.

Rejected: combining the two. Two control loops on one resource fight each other, and the duplicate
config remains.

Rejected (rev1, after MoA): an AIMD "effective cap" with persisted state. Running workers are never
killed, so shrinking a cap only means "spawn nothing until they drain", which pressure-gated pacing
already does.

Reused from prior art as ideas, not imports: PSI line-parsing semantics, hold/recover hysteresis,
fail-safe config parsing.

## 4. Scope of the controller

- Embedded gateway dispatcher (the default, and what tower runs): full controller and sub-passes.
- Deprecated `run_daemon` and one-shot `hermes kanban dispatch`: static in v1. No controller, no
  sub-pass. Each prints a note when `adaptive_admission.mode` is not `off`, and neither is gated by
  the allowance, so either can exceed the paced rate if an operator runs it alongside (documented;
  MoA I12). Rev1 put the controller in `run_daemon`; rev2 drops that to avoid a second copy of the
  loop in a deprecated path.

## 5. Control law: pressure-gated paced admission

"Slots grow as work queues" means: the host's running count grows by at most `step` new workers per
`settle_seconds` while there is spawnable work, no CPU contention and enough memory headroom, up to
the ceiling. "Back off" means admitting zero until the condition clears, while running workers drain
naturally. Running workers are never signalled.

### 5.1 Modes

`mode: off | shadow | enforce` (MoA I1).
- `off` means today's dispatcher, byte-for-byte: no sub-passes, no decisions, no status file.
- `shadow` computes every decision on the full-tick and sub-pass schedule, writes the status file,
  logs transitions and counts "would-hold" time. It passes `spawn_allowance=None` and runs NO
  sub-pass spawns, so admission stays exactly today's.
- `enforce` applies the allowance and runs sub-passes.

### 5.2 Cadence (card item 1): option (b), admission sub-pass

The full tick keeps its interval (`dispatch_interval_seconds`, default 60) and all its work:
reclaim, promote, zombie reap, auto-decompose, health telemetry, tick hook and admission. The
inter-tick wait becomes one helper, `_inter_tick_wait(...)`, with a single call-site change in
`_kanban_dispatcher_watcher` (MoA I11):

    full tick: decision = controller.decide(now_full); tick_once(spawn_allowance=...)
               controller.record(decision.now, spawned_total)
    next_full = t_full + interval
    next_check = t_full + settle          # anchored to DECISION times, not spawn completion
    while running and monotonic() < next_full:
        sleep 1 s slices until min(next_full, next_check)
        if monotonic() >= next_full: break
        settings = parse(live config)           # mtime-cached, last-known-good on parse error
        next_check = next_check + settings.settle_seconds   # re-derived from LIVE settle
        if settings.mode != enforce or paused or nothing_admissible_latched: continue
        decision = controller.decide(now)
        if decision.allowance and host_has_admissible_work():
            res = tick_once(admission_only=True, spawn_allowance=decision.allowance)
            controller.record(decision.now, spawned_total(res))
            if spawned_total(res) == 0: latch nothing_admissible (fingerprints of all boards)

Rules:
- Liveness (MoA B3): the helper is used in every mode. With `mode: off` it only re-reads settings at
  each check and never decides or spawns, so switching `off` -> `enforce` takes effect within one
  `settle_seconds` (default 5 s). The `off` path's I/O is one mtime-cached config read per 5 s.
  Pause and mode changes are seen at the next check. Changes to `settle_seconds` apply from the next
  check.
- Pacing (MoA B2): `record(decision.now, n)` stamps the DECISION time, never spawn completion. The
  pacing test is `now - last_grant_at >= settle_seconds - PACING_SLACK`, with `PACING_SLACK = 0.5 s`,
  a module constant (half a sleep slice), so a spawn that took 1.1 s or a 1 s slice boundary can't
  push the next grant out a whole extra interval. Under unbounded backlog, enforce mode spawns on 11
  or 12 of the 12 checks per 60 s interval: 11 sub-passes plus the full tick.
- Full-tick interaction: the full tick is the first decision point of each interval and resets
  `next_check`. A grant used at the full tick paces the first sub-pass exactly like a sub-pass grant.
- Trigger predicate (MoA I5): `host_has_admissible_work()` = `has_spawnable_ready` OR (review
  dispatch enabled AND `has_spawnable_review`) on ANY board, the same probe as today's
  `ready_nonempty`, read-only. Because that probe ignores respawn guards and per-profile caps, a
  sub-pass that spawns 0 with a positive allowance latches `nothing_admissible` together with the
  board fingerprints (`board_db_fingerprint`). Sub-passes are then skipped until the next full tick
  or until any board's `(mtime_ns, size)` changes. A fully guarded or capped backlog therefore costs
  one write-lock sub-pass per interval, not one per 5 s.
- Sub-pass content: `dispatch_once(conn, ..., admission_only=True, spawn_allowance=n)` per board,
  passing the REMAINING host allowance to each board in turn. It takes the same per-board
  `_dispatch_tick_lock`. It SKIPS `_run_reclaim_phase`, the WAL checkpoint and
  `on_kanban_dispatch_tick`. It keeps `_tick_spawn_budget` (ceiling, memory tiers), the per-profile
  cap, the review reservation and `_dispatch_lane_task` (respawn guard, claim, workspace, spawn,
  `on_kanban_worker_spawned`). It checks the respawn guard WITHOUT writing the `respawn_guarded`
  event. The next full tick records it once, as today.
- Threading: sub-passes use the same `_to_thread_process_service` as full ticks, off the event loop.
  One coroutine runs both, so they never overlap, and there is no new task, thread or lock.
- Read order in a sub-pass: signals and settings first (µs), then the admissible-work probe, then
  the all-boards running count. A held or idle host touches no board DB beyond the probe.
- Promotion latency: completion-time `recompute_ready` promotions are visible to the next sub-pass.
  Reclaim-only promotions (stale/crash requeues) wait for the next full tick, as today.
- Skipping reclaim can only OVERCOUNT `running`, which is conservative.
- Health telemetry (MoA I4 caller side): `bad_ticks` counts full ticks only. A full tick is not
  "bad" if any spawn happened since the previous full tick.

Why (b):
- (a), a 5 s full tick, is cheap (0.14 s) but multiplies every per-tick side effect by 12:
  `respawn_guarded` event volume, auto-decompose LLM calls, tick-hook invocations, and the 6-tick
  "stuck" window (6 min -> 30 s). Those are documented behaviors outside this card. Rejected for
  scope, not cost.
- (c), a re-tick after a spawn, misses work that arrives while idle and pressure that clears
  mid-interval, and each re-tick has (a)'s side effects. Rejected.

### 5.3 Ceiling

`ceiling` is the value `_tick_spawn_budget` actually enforces in the embedded loop:
`settings.max_in_progress` (the boot snapshot until #117755 lands). The controller never computes
its own, so its min-running clamp and status report cannot disagree with enforcement. When
`kanban.max_in_progress` is null, that is the derived default (2..8, 8 on tower), and startup logs
`kanban admission: ceiling=8 (derived; set kanban.max_in_progress to grow further)` at WARNING when
the mode isn't `off` (MoA I12).

### 5.4 Signals (Linux)

- `cpu_psi` = `/proc/pressure/cpu` `some avg10` (%) — control
- `mem_avail`, `mem_total` = `/proc/meminfo` via the existing `_system_memory_sample()` — control
- `mem_level` = the existing `_memory_pressure_level()` (MemAvailable tiers) — control
- `mem_psi` = `/proc/pressure/memory` `some avg10` — recorded in status and shadow counters only

Activation: the controller is active only when `cpu_psi`, `mem_avail` and `mem_total` are readable
and the ceiling isn't None. Any missing input makes it inactive: `allowance = None` (today's
behavior, including today's per-tick burst up to the ceiling), reason `admission_inactive:<which>`,
and a WARNING logged once per distinct reason while the mode isn't `off` (MoA I7). There is no
per-signal partial mode: the headroom floor can't be evaluated without MemAvailable, and running
CPU-only would silently drop the memory guard.

avg10 rather than avg60 (card item 2): decisions happen every 5 s, avg10 is the kernel window that
matches that cadence, and the kernel updates it every 2 s. avg60 lags badly (section 2.4): with it,
admission would stay held for about a minute after every spike. Rev1's aliasing argument for avg60
assumed a 60 s tick. At a 5 s cadence, a missed sub-5 s burst admits at most `step`, and the next
read sees it.

Deliberately excluded (unchanged): load per core and swap-out growth.

### 5.5 Headroom floor (card item 4): the leading memory guard

    worker_bound   = tools.process_registry.worker_memory_max_bytes()   # 4 GiB on tower; cached per process
    headroom_floor = min(max(headroom_min_gib, headroom_worker_multiple * worker_bound),
                         mem_total / 2)
    # tower: min(max(8, 4 * 4), 31.2) = 16 GiB

Admission holds while `mem_avail < headroom_floor`.
- Leading: MemAvailable falls as workers grow, before the kernel stalls.
- Sized for the case that bites: `worker_bound` is the per-worker MemoryMax, which suite-running
  workers actually reach. The multiple of 4 reserves room for four idle workers going to full suites
  together (4 heavy of 19 observed). The card's `M x p95 current RSS` suggestion was rejected
  because the live p95 is dominated by idle LLM-bound workers when few suites run, exactly when a
  burst of suite starts would find the floor too low. The ceiling is also stable and needs no scope
  scan.
- The floor is fixed; the risk grows with `host_running` (MoA I3). At 64 running and a 21 % heavy
  fraction, about 13 workers could turn heavy. Rev2 deliberately does not scale the floor with
  `host_running`, because that would need an `f_heavy` input nobody has measured under the real mix,
  and because the floor only affects admission. The aggregate bound on post-admission growth is the
  ceiling (section 1.1). Shadow mode records the maximum concurrent count of scopes above 2 GiB so
  the install card can size both.
- The `mem_total / 2` clamp (a module constant) keeps small hosts from getting a floor they can never
  satisfy. On a 16 GiB host that is 8 GiB.
- `worker_memory_max_bytes()` is a public alias the BUILD adds for the existing private function (no
  behavior change). Without systemd scopes it still returns the bound Hermes would apply (min(phys/2,
  4 GiB), default 1 GiB).
- Tower ordering: the floor (16 GiB = 26 %) holds before the elevated tier (15 % = 9.4 GiB) goes RED.
- Current margin: at ~42 GiB available with ~20 workers, the floor leaves ~26 GiB of admission room,
  ~110 idle workers at ~240 MiB anon. The ceiling (64) binds first.

### 5.6 Classification

- RED if `cpu_psi >= cpu_psi_backoff` or `mem_level in {elevated, critical}`.
- AMBER (hold) if not RED and (`cpu_psi >= cpu_psi_hold` or `mem_avail < headroom_floor`). The
  trigger names `cpu_psi` or `headroom`.
- GREEN otherwise.

CPU pair `hold 30 / backoff 60` (avg10). Quiet-host max was 2.2. One full suite on 15/20 cores peaked
at 23.2 and never reached 25, so a single suite doesn't hold admission of LLM-bound workers (0.02
cores each; MoA I2). Two concurrent suites are expected to reach 30 and can reach 60 when the host
saturates. That's unmeasured, and shadow mode checks it before enforce (section 8).

Hysteresis: `hold < backoff` for the CPU pair (validated). GREEN requires every signal below hold,
so a value between hold and backoff freezes admission without a cooldown. avg10 is an exponential
average (tau = 10 s): from 60 it decays below 30 in ~7 s after the stall stops.

### 5.7 Allowance

1. RED: `allowance = 0`, `last_red_at = now`.
2. AMBER: `allowance = 0`.
3. GREEN inside the cooldown (`now - last_red_at < backoff_cooldown_seconds`): 0, reason
   `cooldown`.
4. GREEN inside the pacing window (`now - last_grant_at < settle_seconds - PACING_SLACK`): 0,
   reason `pacing`.
5. GREEN otherwise: `allowance = step`.
6. Min-running (liveness): if `host_running < min_running` and the level isn't RED from `mem_level`,
   `allowance = max(allowance, min_running - host_running)`. CPU holds and the headroom floor never
   starve the host below `min_running` workers; the MemAvailable tiers can. If the host count
   errors, the override is skipped (rev1 MoA I5).
7. Downstream order (unchanged guards, each can only lower the budget): allowance ->
   `_tick_spawn_budget` (ceiling minus host running, then memory tiers critical 0 / elevated <= 1)
   -> per-profile cap -> [future per-provider budget, #123654] -> review reservation.

`record(decision_now, n)` wiring (MoA I6): the loop calls it once per DECISION with `n` = the total
`len(res.spawned)` summed over boards. A failed claim or spawn isn't in `res.spawned`, so it neither
starts the pacing window nor consumes the shared allowance for later boards. If `n >= 1`,
`last_grant_at = decision_now`. A grant of 2 that spawns only 1 still starts the window. That's
intended: the window bounds the admission rate, and the 24/min figure is a maximum. If `n == 0`, no
window starts. A min-running override grant larger than `step` also starts the window.

Review reservation with a small allowance: the existing rule holds one slot for spawnable review
work, so allowance 2 -> 1 ready + 1 review, and allowance 1 -> 0 ready + 1 review. Reviews finish
handoffs, so favoring them at the margin is today's policy, unchanged.

Cooldown (card item 5): with avg10, "wait for GREEN" already gives ~7 s of decay after a CPU RED, so
the cooldown adds ~3 s there. For `mem_level` RED it is the only hysteresis: MemAvailable is
instantaneous and bounces as suite phases free and allocate. Kept at Justin's 10 s as a debounce.

Timing uses `time.monotonic()`. State is process-local and not persisted. A restart starts clean,
and the first GREEN decision admits only `step`. Over-cap (`host_running > ceiling`): nothing spawns,
nothing is killed, and the host drains to the ceiling.

### 5.8 Step size (card item 6): `step: 2`

At 5 s, the maximum is 24 workers/min.
- Transient: ~0.85 cores and ~170 MiB per new worker in its first 5 s. At most 2 are in transient at
  a time: 1.7 of 20 cores, ~0.35 GiB.
- Steady growth: 24 x ~240 MiB anon = ~5.6 GiB/min of idle-worker memory. From ~42 GiB, the 16 GiB
  floor is ~4.6 min of continuous admission away. The ceiling (64 - 20 = 44 more, ~10.3 GiB) binds
  first, at ~1.8 min.
- The risk is not the rate. It's admitted workers later starting suites together, which no pacing
  can observe (section 1.1). `step: 1` halves the rate without touching that risk, and filling to 64
  would take ~3.7 min.

### 5.9 Defaults (declared once, section 6)

`mode: off`, `step: 2`, `settle_seconds: 5`, `backoff_cooldown_seconds: 10`, `min_running: 2`
(= `DERIVED_MAX_IN_PROGRESS_FLOOR`, reused as the DEFAULT_CONFIG value), `cpu_psi_hold: 30`,
`cpu_psi_backoff: 60`, `headroom_min_gib: 8`, `headroom_worker_multiple: 4`. The MemAvailable tiers
are the existing `classify_pressure` tiers verbatim. `PACING_SLACK` and the `mem_total / 2` clamp are
module constants.

## 6. Configuration (config.yaml only; no HERMES_* env vars)

A nested block under the existing `kanban:` section in `hermes_cli/config_defaults.py`. Adding a key
to an existing section needs no `_config_version` bump (`hermes_cli/AGENTS.md`).

    kanban:
      max_in_progress: null          # existing; the host ceiling in all modes (unchanged meaning)
      dispatch_interval_seconds: 60  # existing; full-tick cadence, unchanged
      adaptive_admission:
        mode: off                    # off | shadow | enforce
        step: 2
        settle_seconds: 5            # also the check / admission sub-pass cadence
        backoff_cooldown_seconds: 10
        min_running: 2
        cpu_psi_hold: 30             # PSI cpu some avg10, %
        cpu_psi_backoff: 60
        headroom_min_gib: 8
        headroom_worker_multiple: 4  # x per-worker MemoryMax; clamped to MemTotal/2

- Single source of defaults: `DEFAULT_CONFIG["kanban"]["adaptive_admission"]`. The module falls back
  to the DEFAULT_CONFIG value for a bad key, with no duplicate literals.
- Validation is fail-safe (MoA I7). It logs a WARNING once per distinct bad value and never crashes
  the loop.
  - `mode` must be one of the three strings. YAML `false`/`true` are rejected (a warning and `off`)
    so a bare boolean can't silently mean enforce.
  - Numeric keys reject non-numbers, NaN, inf and negatives. Also `step` outside 1..64,
    `settle_seconds` < 1, `min_running` > 64, and PSI thresholds outside 0..100.
  - `cpu_psi_hold >= cpu_psi_backoff` reverts both keys.
  - `min_running` above the ceiling is clamped to the ceiling.
  - `settle_seconds` > `dispatch_interval_seconds` is allowed: sub-passes never run, and pacing
    applies across full ticks.
  - Unknown keys in the block (including rev1's `enabled`, `floor`, `memory_psi_*`) get a WARNING and
    are ignored.
- Parse failure or mid-write read: `load_config_readonly` already returns the last-known-good config
  (`config.py:2337-2347`), so a torn write doesn't flip the mode.
- Liveness: re-read at each check (mtime-cached). Mode and tuning apply within one `settle_seconds`
  without a restart, independent of #117755 (section 5.2).
- 9 keys (rev1 had 9). Upstream may prefer exposing only `mode`, `step` and `settle_seconds`, with
  thresholds as module constants. That's negotiable without changing the law.

## 7. Interactions

- `max_in_progress_per_profile`: unchanged, applied in full ticks and sub-passes via
  `_dispatch_lane_task`. It stays a static operator cap (binding comment). A capped plateau shows as
  `skipped_per_profile_capped` and status `limited_by: per_profile`.
- Per-provider budget (#123654, not implemented): it would be a per-row clamp in
  `_dispatch_lane_task`, keyed on each run's resolved provider/endpoint (task override, else profile
  default), like the per-profile cap. Sub-passes use the same function, so it applies to both paths
  with no change to `kanban_admission.py`. The allowance stays host-level and provider-agnostic, and
  `limited_by` would gain `provider`. Nothing here keys on profile identity as a quota proxy.
- Cross-board host budget: `host_running` counts all boards, the allowance is shared across boards
  in both pass kinds, and `_tick_spawn_budget` still subtracts other boards from the ceiling.
- Existing memory guard: unchanged, applied after the allowance. Adaptive additionally treats
  elevated/critical as RED (which starts the cooldown), and the headroom floor holds earlier.
- Remote-provider rate limits: the existing per-task `rate_limit_cooldown` respawn guard applies in
  sub-passes too. Anything broader is #123654.
- Multiple controllers: the embedded dispatcher is a singleton per kanban home (flock). `run_daemon
  --force` and one-shot dispatch are ungated (section 4). The per-board `_dispatch_tick_lock` still
  serializes each board's writes, sub-passes included.
- Unsupported, and documented as such: several kanban homes sharing one host; containers or cgroup
  CPU quotas (host PSI isn't the container's view).
- `hermes pause` (ESTOP): gates full ticks and sub-passes.

### 7.1 If #117755 has not landed when the BUILD lands

- Adaptive keys are live (re-read at each check).
- The ceiling is the embedded path's boot snapshot. The controller uses that enforced value (section
  5.3). Status and `hermes kanban stats` show `ceiling_configured` (a live read) and
  `ceiling_restart_pending: true` when the two differ. Changing the ceiling needs a gateway restart,
  and the operator can see that.
- Other `kanban.*` settings (per-profile cap, interval) keep their boot snapshot, as today.
- Switching `mode` to `off` is live and always safe: it only removes a clamp and the sub-passes.
  Only a CEILING change needs a restart (MoA I10).
- When #117755 lands, `settings.max_in_progress` becomes live and reaches the controller through the
  same argument, so no rev2 code change is needed. The BUILD passes the allowance as an explicit
  `tick_once(...)` argument and never replaces `self.settings`, so it doesn't collide with the PR's
  settings swap. If #117755 is still DIRTY at BUILD start, the BUILD lands independently and leaves a
  comment on #117755 naming the one shared call site (`_inter_tick_wait`) (MoA I11).

## 8. Landing and rollout

- Build: a separate approved BUILD card on branch `feat/kanban-adaptive-admission` from fresh
  `origin/main` on the tower dev clone. TDD, repo gates, reviewscope plus specialist reviews.
- Upstream: shaped as a NousResearch PR (default `off`, rung-1 extension, no new tool or env var).
  Push/PR need Justin's explicit approval (shared org repo). If upstream declines, it becomes a
  personal patch on the `deployed` branch (plan t_75b37fdf).
- Tower install (a later approved card only), in three phases:
  1. Baseline + shadow (24 h, MoA B4/I1): deploy the build with `mode: shadow` and the current static
     ceiling. One gateway restart loads the code. A small read-only recorder in the install card
     (not the BUILD) samples each second: PSI avg10 cpu/mem, MemAvailable, host running, ready
     count, and the count of scopes above 2 GiB. Shadow decisions come from the status file and log
     counters. The report gives time in each level by trigger, would-hold time while admissible work
     existed, p95 ready wait under the static cap (baseline), maximum concurrent heavy scopes, and
     minimum MemAvailable.
  2. Gate to enforce, with numeric bounds:
     - would-hold-with-admissible-work <= 10 % of backlog time;
     - no shadow RED whose logged trigger is below its threshold (logic check);
     - at least one observed multi-suite period classified AMBER or RED (the thresholds actually
       bite). If none occurs in 24 h, run two suites deliberately and check.
     If any bound fails, retune thresholds in config (live) and repeat shadow. The ceiling for
     enforce comes from the section 1.1 choice and the heavy-scope data.
  3. Enforce canary (24 h): `mode: enforce`.
     - Controller-held-as-designed criteria (pass/fail on logic): under sustained backlog with no
       hold, host running reaches min(ceiling, backlog) within ~5 min. Every AMBER/RED names a
       trigger at or above its threshold. At most 1 sub-pass per `settle_seconds`, 0
       `respawn_guarded` events written by sub-passes, and event volume within 10 % of baseline. The
       status file's `updated_at` age stays <= 3 x interval. No worker is ever signalled by the
       dispatcher. p95 ready wait <= the shadow-phase baseline.
     - Tier/ceiling criteria (retune the ceiling, not the controller) (MoA I9): MemAvailable drops
       below the elevated tier (15 %); a HOST OOM kill (`/proc/vmstat` `oom_kill` delta, or root
       `memory.events oom_kill`), which is distinct from a scope `oom_kill` at its own 4 GiB
       `memory.max`, which is per-worker and expected under suites.
     - If the enforced ceiling is below the current running count at the switch, the host drains to
       it with no spawns. Pass if it never spawns above the ceiling during the drain.
- Rollback: set `mode: off` (live, no restart; always safe). If the ceiling was raised for the
  rollout, set `kanban.max_in_progress` to the measured safe static value the shadow phase recorded
  (not blindly the pre-rollout value) and restart the gateway while #117755 is unmerged (MoA B1).
  The runbook states both steps.

## 9. Code shape (for the later BUILD card; LOC estimate ~300-380 plus tests, non-binding)

- NEW `hermes_cli/kanban_admission.py`:
  - `AdmissionSettings` + `parse_admission_settings(kanban_cfg)`: pure.
  - `HostSignals` + `read_host_signals()`: the module-level test seam. It reads
    `/proc/pressure/{cpu,memory}` (bounded, 4 KiB cap) and parses `some avg10`. MemAvailable and
    MemTotal come from `_system_memory_sample()`, each sample stamped with `sampled_at`. Never
    raises.
  - `parse_psi_some_avg10(text) -> float | None`, `headroom_floor_bytes(settings, worker_bound,
    mem_total) -> int`, `classify(signals, settings, headroom_floor) -> (level, trigger)`: pure.
  - `AdmissionController.decide(now, signals, settings, ceiling, host_running) -> Decision
    (allowance | None, level, reason, now)` and `.record(decision_now, n)`: pure given inputs.
    Shadow counters (seconds per level/trigger, would-hold-with-work seconds).
  - `write_status(...)` -> `<kanban_home>/kanban/admission_status.json` via
    `utils.atomic_json_write`, mode 0644 like the sibling kanban files. It contains no secrets.
    Written on every full tick and on a change of LEVEL or TRIGGER only. Pacing and allowance flips
    don't trigger writes, which gives about 1/min plus transitions (MoA I8). It holds `updated_at`,
    `sampled_at`, `pid`, `mode`, `level`, `reason`, `allowance`, `ceiling`, `ceiling_configured`,
    `ceiling_restart_pending`, `host_running`, `headroom_floor_bytes`, the signal values (including
    `mem_psi`), `limited_by`, and the shadow counters. Observability only, never read back for
    control. Machine-global under `kanban_home()`.
- `tools/process_registry.py`: a public `worker_memory_max_bytes()` alias (no behavior change); the
  admission module caches its result per process.
- `hermes_cli/kanban_db_dispatch.py`:
  - `count_running_tasks_all_boards() -> Optional[int]`: None on any board error. The existing
    function keeps its fail-open behavior.
  - `dispatch_once(..., spawn_allowance: Optional[int] = None, admission_reason: Optional[str] =
    None, admission_only: bool = False)`. `admission_only` skips `_run_reclaim_phase`, the WAL
    checkpoint and the tick hook, and passes `record_guard_events=False` to `_dispatch_lane_task`.
    `_tick_spawn_budget` clamps to `spawn_allowance` when it isn't None and records
    `result.admission_hold` when that was binding. Defaults leave behavior unchanged.
  - `describe_suppression` adds `admission=<reason>` when set.
  - `run_daemon` and one-shot dispatch: print the static-mode note only.
- `gateway/kanban_watchers_dispatcher.py` `_KanbanDispatcher.tick_once(spawn_allowance=None,
  admission_reason=None, admission_only=False)`: iterates boards with the REMAINING host allowance,
  decrementing by `len(res.spawned)`. Plus `host_has_admissible_work()` (the existing
  `ready_nonempty`) and `board_fingerprints()`.
- `gateway/kanban_watchers.py`: one `AdmissionController` in `_kanban_dispatcher_watcher` (lock
  holder only). `_sleep_between_ticks(interval)` at `:324` is replaced by `_inter_tick_wait(...)`
  (section 5.2). `spawned_since_last_full_tick` feeds `bad_ticks`.
- Exposure:
  - Log: one INFO anchor line per start (`kanban admission: mode=enforce ceiling=64 step=2/5s
    window=avg10 headroom_floor=16.0GiB`) plus the section 5.3 WARNING for a derived ceiling.
    Transitions are coalesced: the first RED after non-RED logs immediately at WARNING with the
    trigger and threshold. Other transitions log at INFO at most once per full interval (the latest
    state and a transition count). The inactive WARNING logs once per reason.
  - `hermes kanban stats` (+ `--json`): an `admission` block from the status file (fields above,
    `age_seconds`, `stale` when older than 3 x `dispatch_interval_seconds`). It's printed only when
    the mode isn't `off`, so `off` output is unchanged.
  - Dashboard: explicit follow-up (not part of this plan).
- Docs: `website/docs/user-guide/features/kanban.md` rows for `kanban.adaptive_admission.*`, plus
  notes that `max_in_progress` is the ceiling, that sub-passes run every `settle_seconds` in enforce
  mode, and that `run_daemon`/one-shot dispatch stay static.

BUILD preconditions (re-verify with file:line before coding; MoA I4):
- `tick_once_for_board` kwargs (`kanban_watchers_dispatcher.py:184`).
- `DispatchResult.spawned` / `.respawn_guarded` shapes (`kanban_db_dispatch.py:100-160`).
- `_dispatch_lane_task`'s only per-row writes on the non-spawn paths are the guard event (`:2036`)
  and `_apply_default_assignee` (runs before the lane; it is a real assignment and stays in
  sub-passes). T26 asserts row counts on `tasks`, `task_events` and `task_runs`.
- `check_respawn_guard` is SELECT-only (`:1491-1600`).
- Full ticks run through `_to_thread_process_service` (`kanban_watchers.py:284-304`).
- Ancestor slices have no `memory.max`. If a future host sets one, the BUILD documents it as
  unsupported; it adds no slice math.
- `_deep_merge` for a partial nested block (plus a test); `atomic_json_write`; `recompute_ready` on
  completion; #117755's merge state.
- Provision a test venv first (`scripts/run_tests.sh` failed here: "activate: no bootstrap Python
  found; run setup-hermes.sh").

## 10. Tests (TDD; behavior contracts, injected clock and signals, no live /proc)

New `tests/hermes_cli/test_kanban_admission.py` (pure, table-driven):

| # | Test | Mutant it must kill |
|---|------|---------------------|
| T1 | GREEN, pacing elapsed -> allowance == step. After `record(decision_now, 1)`, a decision at +4.0 s -> 0; at +4.6 s (within slack) -> step; at +5 s -> step. Row: spawn completes 1.1 s after its decision, next check at decision+5 s -> step (stamped with the decision time, not completion) | grant ignores step; window stamped at completion; slack removed; window started on an unused grant |
| T2 | GREEN with `record(now, 0)` -> next decision still step | window starts on decide or on n=0 |
| T3 | each hold alone -> AMBER, 0, no cooldown: cpu_psi at hold; mem_avail 1 byte below the headroom floor | a signal dropped from AMBER; AMBER starts cooldown |
| T4 | each RED alone -> RED, cooldown started: cpu_psi at backoff; mem_level elevated; mem_level critical | a signal dropped from RED |
| T4b | mem_psi = 100 with everything else GREEN -> GREEN (recorded, not a control) | memory PSI wired into control |
| T5 | GREEN within 10 s of RED -> 0 (`cooldown`); after -> step | cooldown removed; wrong timestamp |
| T6 | cpu_psi oscillating between hold and backoff for 20 decisions -> always 0, no cooldown | hysteresis collapsed |
| T7 | ceiling respected (integration, T15) | allowance overrides ceiling |
| T8 | min_running: host_running < min_running under CPU RED or headroom AMBER -> min_running - running; under mem_level RED -> 0; an override grant larger than step starts the pacing window | override removed; override beats the tiers; override doesn't pace |
| T9 | override skipped when the host count is None | undercount treated as 0 |
| T10 | inactive (allowance None, `admission_inactive:<which>`) when cpu_psi is None; when mem_avail is None with mem_total present; when the ceiling is None | partial-signal mode; headroom evaluated on missing data |
| T11 | macOS-shaped signals (all None) -> inactive | macOS regression |
| T12 | settings validation: each rule in section 6 -> default + one WARNING; `mode: true` -> off; NaN/inf rejected; hold >= backoff -> both default; min_running > ceiling -> clamped; a partial block merges with defaults; rev1 keys ignored with a WARNING | validation bypassed; boolean accepted as enforce; merge not recursive |
| T13 | `parse_psi_some_avg10`: real fixture; `full` line ignored; avg60/avg300 not used; malformed/oversized/empty -> None; never raises | wrong field; parser raises |
| T14 | restart: a new controller, GREEN -> exactly step | init grants ceiling |
| T24 | `headroom_floor_bytes`: 4 GiB bound, 62.5 GiB total -> 16 GiB; 4 GiB bound, 16 GiB total -> 8 GiB (clamp); 1 GiB bound, 62.5 GiB total -> 8 GiB (min) | multiple ignored; min ignored; clamp missing or inverted |
| T29 | shadow mode: `decide` produces the same levels and reasons as enforce, the loop passes `spawn_allowance=None`, and the would-hold counters accumulate | shadow enforces; counters not accumulated |

Integration (real `dispatch_once` against temp HERMES_HOME boards; fixtures `kanban_home`,
`all_assignees_spawnable`):
- `tests/hermes_cli/test_kanban_host_cap.py`:
  - T15: `spawn_allowance=2` with 10 ready rows and a large ceiling spawns exactly 2 and sets
    `admission_hold`.
  - T16: defaults (`spawn_allowance=None`, `admission_only=False`) behave exactly as today; existing
    tests stay green unmodified.
  - T17: `run_daemon` passes no allowance and runs no sub-pass in any mode (static v1).
  - T18: the host allowance is shared across two boards (total spawned <= step) in both pass kinds.
    A board whose claim fails does not consume the allowance for the next board.
  - T25: `admission_only=True` does not call `_run_reclaim_phase` or the WAL checkpoint (spies), does
    not fire `on_kanban_dispatch_tick`, does fire `on_kanban_worker_spawned`, and honors the ceiling,
    memory tiers, per-profile cap and review reservation (allowance 1 with spawnable review -> 0
    ready + 1 review).
  - T26: `admission_only=True` with a respawn-guarded row appends to `result.respawn_guarded` and
    leaves the row counts of `tasks`, `task_events` and `task_runs` unchanged. A full tick writes
    exactly one guard event.
- `tests/hermes_cli/test_kanban_memory_guard.py`: T19, critical/elevated behavior unchanged with an
  allowance present.
- NEW `tests/gateway/test_kanban_adaptive_admission_watcher.py` (injected monotonic clock and a sleep
  seam; no wall-clock timing):
  - T20a (#117755 absent): switching `mode` enforce -> off with a config mtime bump stops sub-passes
    and the allowance at the next check (<= settle). The enforced ceiling stays the boot value, and
    the status shows `ceiling_restart_pending: true` with the edited value. Actual values asserted.
  - T20b (skip-marked until #117755 is on the base): a live `settings.max_in_progress` change reaches
    `decide` in the same tick.
  - T21: allowance 0 with running workers: nothing spawned, nothing signalled (spy on
    spawn/terminate).
  - T27: enforce, interval 60, settle 5, unbounded spawnable backlog, instant spawns: between 11 and
    12 spawning decisions per 60 fake seconds (full tick + sub-passes) and never two within 4.5 fake
    seconds. No sub-pass when the admissible-work probe is False, when paused, in `shadow`, or in
    `off`. `off` -> `enforce` via a config edit takes effect within <= 5 fake seconds.
  - T28: health telemetry: a full tick with ready work and 0 spawns doesn't increment `bad_ticks`
    when a sub-pass spawned since the previous full tick; it does when nothing spawned.
  - T30: nothing-admissible latch. A sub-pass with allowance > 0 that spawns 0 (all rows guarded)
    stops further sub-passes until the next full tick. A board fingerprint change (new ready row)
    re-enables them before then.
- Stats: T22, `hermes kanban stats --json` has an `admission` block when the mode isn't `off`
  (including `ceiling_restart_pending` and the shadow counters). Output is unchanged when `off`. In
  `tests/hermes_cli/test_kanban_core_functionality.py`, or wherever the BUILD finds existing stats
  coverage.
- T23: `describe_suppression` includes `admission=<reason>`.
- T31: the status-file write rule. Under a backlog alternating pacing/GREEN, writes happen once per
  full tick plus level/trigger changes only, and `sampled_at` is present.

Hermeticity: an autouse patch in `tests/conftest.py` beside `:437` makes
`kanban_admission.read_host_signals` return all-None signals, so no test reads live PSI and the
default is "inactive".

Mutation check: the BUILD runs a manual mutant pass for T1-T14, T24 and T27-T31 (apply each listed
mutant, confirm the named test fails, revert) and records the table in the review packet.

## 11. Exact serial test commands (never the full suite)

    scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_admission.py
    scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_host_cap.py tests/hermes_cli/test_kanban_memory_guard.py tests/hermes_cli/test_kanban_per_profile_cap.py
    scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py tests/hermes_cli/test_kanban_dispatch_tick_hook.py tests/hermes_cli/test_kanban_dispatch_lock.py
    scripts/run_tests.sh -j 1 tests/gateway/test_kanban_adaptive_admission_watcher.py tests/gateway/test_kanban_auto_decompose_secret_scope.py tests/gateway/test_kanban_reconcile_orphans.py
    scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_core_functionality.py
    scripts/run_tests.sh -j 1 <the process_registry test file the BUILD locates for the worker_memory_max_bytes alias>

All existing files listed above were verified to exist on d0288be5b3.

## 12. Discoveries and dispositions

- D1: the embedded dispatcher's caps are boot-stale. EXISTING #117734 + PR #117755 (OPEN,
  CONFLICTING/DIRTY). Reuse; out_of_scope_followup. Behavior without it: section 7.1.
- D2: docs drift, `kanban.md:999` "unset (unlimited)" vs the derived 2..8 cap. EXISTING #123652,
  fix PR #123666 (t_1027725b). Tracked; no rev2 work.
- D3: docs claim `GET /api/plugins/kanban/inspect` (`kanban.md:1042`); no such route. EXISTING
  #123653, same PR #123666. Tracked. (The dashboard admission view is still a separate follow-up.)
- D4: per-provider budget is EXISTING #123654 (separate card). Compatible (section 7). Tracking
  resolved.
- D5: macOS memory guards inert, EXISTING #119870. Adaptive is inactive there (T11).
- D6: deprecated `run_daemon --force` takes no flock (already warned, `kanban_ops.py:195-197`). Now
  also ungated by adaptive (section 4). No new issue.
- D7 (new): host memory PSI includes per-scope limit reclaim. A design input: in_scope_required
  (memory PSI removed from control, section 5.4). No issue.
- D8 (new): `scripts/run_tests.sh` fails in a fresh Kanban worktree without a bootstrap Python.
  out_of_scope_followup. Not filed: first confirm it reproduces outside tower worktree provisioning.
  It is a BUILD precondition.
- D9 (new, fact correction): the live boot cap is 64 / per-profile 16 since 00:05:39 PDT, not 32.
- D10 (new): `ready_nonempty`/`_has_spawnable` ignore respawn guards and per-profile caps, so
  today's "stuck" telemetry can fire on a fully guarded backlog. Pre-existing and outside this card.
  out_of_scope_followup; issue PENDING (upstream creation needs approval). Rev2 works around it for
  sub-passes with the nothing-admissible latch and doesn't change the telemetry.

## 13. Open items (none blocking the BUILD)

- O1: section 1.1 ceiling choice, made at approval. It affects only the install card.
- O2: CPU thresholds and `headroom_worker_multiple` rest on one suite spike and one snapshot. The
  shadow phase measures multi-suite behavior and concurrent heavy scopes before enforce.

## 14. MoA review receipt and reconciliation

The MoA gate RAN on rev2 draft v1. The helper exited 0 and returned 4 blockers, 13 improvements and
10 preferences.
- Input: `receipts/2026-09-26-kanban-adaptive-concurrency-rev2.v1.md`,
  sha256 757c49cad805b8f0014a4e8ceff95f69acc6d3ab7c8ab30ce35f5b14c1967e9b.
- Review: `receipts/2026-09-26-kanban-adaptive-concurrency-rev2.moa-v1.md` (exit code in `.err`).
- Evidence: `receipts/2026-09-26-rev2-evidence/`.

| Finding | Disposition |
|---|---|
| B1 rollout/rollback contradicts the safety math | Accepted. The design is stated plainly (s0). The ceiling choice is made explicit with options and a recommendation (s1.1). Rollback goes to a measured safe static value (s8). The v1 arithmetic is corrected (2.5 %, not 5 %). |
| B2 pacing stamp halves the rate | Accepted. Decision-time stamping, `PACING_SLACK`, schedule anchored to decisions, full-tick interaction defined (s5.2, s5.7). T1 delayed-spawn row; T27 lower bound. |
| B3 enable latency contradicts the disabled path | Accepted. One `_inter_tick_wait` in all modes; `off` re-reads settings every settle without deciding. T27 live-edit row. |
| B4 no evidence of hold frequency | Premise partly false: one suite held at >= 20 for 4 % of its run, not the whole run (`spike_replay.txt`). Remedy adopted: shadow phase with numeric gates before enforce (s8). CPU hold raised to 30. |
| I1 shadow mode | Adopted (`mode: off/shadow/enforce`, T29). |
| I2 CPU thresholds / drop memory PSI | Adopted: hold 30 / backoff 60; memory PSI recorded only (s5.4, s5.6). |
| I3 fixed floor vs growing risk | Accepted as a documented limit (s5.5, s1.1). Scaling rejected for now: it needs an unmeasured `f_heavy`, and shadow data informs the ceiling instead. |
| I4 BUILD preconditions | Verified now: off-loop threads, result fields, SELECT-only guard, ancestor slices unlimited, config-version rule location. Listed in s9. T26 row counts. |
| I5 trigger predicate | Adopted: host-level probe incl. review; nothing-admissible latch with fingerprints (T30). The underlying telemetry gap is D10. |
| I6 record wiring | Adopted (s5.7): once per decision, failed spawns excluded, partial-grant intent, review reservation at small allowances (T18, T25). |
| I7 failure/inactive semantics | Adopted: last-known-good config (verified), strict mode enum, NaN/inf/range checks, no partial-signal mode, inactive WARNING, inactive = today's bursts (s5.4, s6, T10, T12). |
| I8 status write rule | Adopted: writes on full tick + level/trigger change; `sampled_at` (T31). |
| I9 canary criteria split | Adopted (s8 phase 3), including host vs scope OOM attribution and the drain case. |
| I10 rollback wording | Adopted (s7.1, s8). |
| I11 #117755 collision | Adopted: single helper/call site, T20a/T20b, comment on the PR if DIRTY. |
| I12 ungated paths, derived ceiling | Adopted (s4, s5.3). |
| I13 arithmetic consistency | Adopted: anon vs memory.current and idle vs lifetime CPU distinguished (s2.3, s2.4, s5.5, s5.8). |
| Pref: rename floor | Adopted (`min_running`). |
| Pref: fewer upstream keys | Noted (s6); tower tuning keeps them configurable for now. |
| Pref: run_daemon static | Adopted (s4, T17). |
| Pref: sub-pass read order | Adopted (s5.2). |
| Pref: cache worker bound | Adopted (s9). |
| Pref: override resets pacing test | Adopted (T8). |
| Pref: staleness from last decision | Kept 3 x interval: status is guaranteed to be written every full tick, so `updated_at` age measures the same thing. |
| Pref: status permissions | Adopted (0644, no secrets). |
| Pref: LOC non-binding | Adopted. |
| Pref: record dispositions here | This table. |

No second MoA run. Every blocker was either accepted with a concrete remedy or shown partly false
by the recorded measurement, and the skill reruns the gate only on new evidence that forces a
material design change. The shadow-mode addition was the reviewer's own recommendation.
