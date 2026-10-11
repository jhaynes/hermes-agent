# Candidate review-accounting seam contract — `seam-contract/v2`

Status: **frozen** (U0, task t_9b97698a, 2026-10-10)
Authorized by: plan v3 `plan-rca-p1-upstream-first-v3.md`, sha256 `1f7cd6d13c65457581d84e8f30810549f177cdea9c2a9c18462b668183ec5e7e`
Governing plan: `2026-09-25-active-time-accounting-review-leases-plan-v2.md` ("plan v2"), sha256 `438b274d95ccfbe5972ea186db4e59d10d780796254d397412d55d0ffa81dcf9`
Base candidate: `e2427daf3d7f5ef90b0dc196d89fee4a2debb0d4` (`jhaynes/hermes-agent`, branch `backup/bounded-review-feedback` tip)
Phase A evidence: receipt `phase-a-candidate-seam-receipt-e2427daf.json`, sha256 `78287727b9281e5d8a7ce4bb8c19da6ffc8753c8c8d455a589cff73cf199b200`, verdict `candidate_seam_contract_failed` — all 25 behavioral probes passed; the failure is exactly five structural contract gaps.

This document is a forward-looking spec, not an implementation. It freezes, before any code is written on the candidate, what the five missing capabilities must provide natively: caller, preconditions, atomic persistence, generation/watermark semantics, idempotency, typed failures, and readback — plus the re-derived 25-probe behavioral surface that already passed at e2427daf and must keep passing.

Amendment governance: amendments are recorded as decisions with the contract version bumped (`v3`, ...) and the digest of this directory recomputed. U6 judges the candidate against one specific contract SHA; a passing U6 receipt re-binds Phase B to that contract via the minimal named plan-v2 amendment. There is no silent verbatim carry-forward.

Machine-readable form: `seam-contract-v2.schema.json` (this directory) names every probe ID so U6 can diff contract vs receipt mechanically.

## Candidate baseline at the pin (verified against e2427daf source)

| Seam point | Location at e2427daf | Status |
|---|---|---|
| Config source `kanban.review_feedback` | `hermes_cli/kanban_review_state.py:290-298` — reads only `rounds`, `recovery`, `active_seconds` | gap 1: no `termination_reserve_seconds` |
| Action deadline | `claimed()` at `kanban_review_state.py:169` — `time.time()+cap`, wall clock | gap 4: wall-clock, not boot-scoped monotonic |
| Worker spawn | `kanban_db_dispatch.py:2915` — `start_new_session=True` (worker is session+pgid leader) | gap 2: group exists but is never verified/recorded/enumerated |
| Clock checkpoint | `settle_clock()` — `review_clock(attempt_id, boot_id, monotonic_at, wall_at)` per attempt | exists; lease bounds not derived from this domain |
| Cohort tables | `kanban_review_cohort.py` `review_rounds`/`review_members` — lane membership only | gap 3: no generation/watermark/completeness/fence |
| Action states | `reserved -> running -> complete/failed/cancelled` | gap 5: arming and release are one dispatcher tick |
| Enrollment gate | `claimed()`/`released()` key on `get_attempt()`; guards refuse unmanaged writes | baseline surface, kept |
| Read-only export | `hermes kanban show <task> --json` (per-task) | baseline surface, kept |
| Board identity | `workflow_board.board_id` (singleton) | baseline surface, kept |

Trust boundary (plan v2:198, restated): this is an integrity boundary against stale/accidental workflow writes, not a sandbox against same-uid code. The gated launcher's single-step check-to-exec design closes the TOCTOU window as far as a cooperative launcher can; adversarial same-uid behavior remains a stated residual, never a prevention claim.

---

## C1 — Configurable termination reserve (unit U1)

Plan v2 basis: lines 147 (launcher subtracts reserve from both enforcement boundaries), 472 (config source, two-stage validation), 474 (`insufficient_safe_window`).

- **Caller:** the gated launcher (C5) and the supervisor; the config loader at `kanban.review_feedback` is the single source. Harness-side read lives in hermes-config Phase B slices, not the candidate.
- **Preconditions:** (a) at config load — `termination_reserve_seconds` is a finite positive integer, and smaller than every issuable lease/cap duration; (b) at every lease issuance — `remaining > reserve`.
- **Atomic persistence:** the effective reserve value is recorded per action in the lease/receipt row in the same claim transaction that issues the lease. Config changes apply only to actions reserved after the change — never retroactively; receipts keep the value each action actually used.
- **Generation/watermark:** n/a.
- **Idempotency:** re-reading config does not rewrite recorded reserves; an action's recorded reserve is immutable once issued.
- **Typed failures:** `invalid_reserve_config` (stage a, config load); `insufficient_safe_window` (stage b, lease issuance — lineage parked, issues no lease, never rounded up into extra execution time; plan v2:474); `reserve_exceeds_boundary` (launcher refuses to start a command whose timeout plus reserve reaches the action-lease or union-cap boundary, plan v2:147).
- **Readback:** the per-action recorded reserve is visible in the lease/receipt row via the supported export; the harness reads the effective value, never the config default.
- **Replaces:** the hard-derived `time.time()+cap` construction (as replumbed by C4/U4).
- **Default:** 60 seconds; never hard-coded in runtime, harness, or tests.

## C2 — Verified process-group containment (unit U2)

Plan v2 basis: line 200 (mandatory proof cases), line 23 (setsid residual accepted, detected and recorded, never claimed prevented).

- **Caller:** the supervisor/dispatcher supervision path only. `start_new_session=True` is kept — it is deliberate and already gives the launcher/worker a private session and process group.
- **Preconditions:** the spawned launcher/worker pgid is verified to match the recorded pid and its start identity before any signal may be sent to the group; killpg invariants hold fail-closed — never signal pgid <= 1, never signal the supervisor's own group, never signal a group whose recorded (pid, start identity) no longer matches the live check.
- **Atomic persistence:** pgid + leader start identity persist in the `armed` transaction (with C5), not at spawn time alone.
- **Generation/watermark:** n/a.
- **Idempotency:** survivor enumeration is a read; repeated scans are idempotent. Re-verification at signal time is mandatory, not cached.
- **Typed failures:** `containment_identity_mismatch` (recorded (pid, start identity) no longer matches — hold lineage with a typed containment-failure receipt instead of signaling); `self_group_refused` (named probe); `survivors_recorded` (graceful termination + SIGKILL escalation inside the U1 reserve, survivors enumerated and recorded; lineage held non-approvable by the policy boundary).
- **Survivor enumeration:** scan `/proc` for processes whose pgid or session id matches the recorded group — `getpgid`/`killpg` cannot find survivors after the leader exits; a `/proc` scan can.
- **Accepted residual:** `setsid`/daemonization escape is detected and recorded where possible (child pgid drift observed at supervision time, best-effort), never claimed prevented. Escaped work can never produce valid evidence after revocation.
- **Readback:** recorded group identity and survivor lists are visible in the receipt/watchdog row via the supported export.
- **Mandatory proof cases (plan v2:200):** child/grandchild, shell wrapper, ignored SIGTERM, parent exit, supervisor/dispatcher death after release, PID reuse, stale-group reuse. PID-reuse and stale-group-reuse probes use deterministic scaffolding (a synthetic group leader held by the probe harness with a recorded start identity; reuse is simulated by cycling recorded identities against a live group, not by racing the OS scheduler).
- **Platform:** Linux-only probes on tower, allowlisted; macOS deferred (plan v2:301).

## C3 — Cohort generation/watermark export and finalization fence (unit U3)

Plan v2 basis: lines 182 (watermark = authorization sequence allocated in the managed-claim transaction; finalization fence), 86 (board identity anchor), 202 (finalization is a native atomic admission fence; the four race cases).

- **Caller:** the managed-claim transaction allocates the authorization sequence; a single native finalization op is the only fence writer; the read-only export is available to the config harness/validator.
- **Preconditions:** export ordering is fixed and tested: sequence allocation -> claim commit -> export snapshot (one complete read of committed state) -> finalization (fixes `finalized_through` and `exhausted_at`) -> receipt eligibility.
- **Atomic persistence:** finalization is ONE transaction: disables further cohort claims, includes every committed claim through the watermark, fixes the complete generation and `exhausted_at`, marks receipt eligibility pending/settled. Isolation: `BEGIN IMMEDIATE` / single-writer semantics on the board DB; claim-vs-fence tests run across separate OS processes, not threads.
- **Generation/watermark:** native cohort exports carry: board identity (`workflow_board.board_id` paired with canonical DB path + filesystem device recorded at creation), authorization sequence (allocated in the managed-claim transaction), export generation, `finalized_through` watermark, completeness flag. An earlier-starting sibling committed before the fence but closed late keeps finalization pending (plan v2:182).
- **Idempotency:** repeating a finalization for the same generation returns the same finalized record; a post-fence claim fails the fence. A copied DB at a different canonical path/device is rejected; a legitimately restored board re-identifies only via a new operator decision record naming the restore (same authority class as enrollment); otherwise path/device mismatch holds.
- **Typed failures:** `cohort_claim_fenced` (post-fence claim), `board_identity_mismatch` (copied DB / path-device mismatch), `finalization_pending` (incomplete generation cannot satisfy the completeness fence), `partial_observation` (partial/stale export issues no lease or advancement, plan v2:472).
- **Readback:** the cohort export is the supported read-only path carrying identity + generation + watermark + completeness; `show --json` disagreement can only place the projection in `reconciliation_required`, never alter native state.
- **Legacy migration default:** legacy cohorts are generation 0, incomplete, not finalizable — a legacy cohort cannot satisfy the fence until re-finalized under the new contract.

## C4 — Boot-scoped monotonic lease bounds (unit U4)

Plan v2 basis: lines 145 (one boot-scoped monotonic domain; comparisons across boot IDs illegal), 147 (boundary rules), 213 (timeout = min of command cap, lease remaining, allowance remaining).

- **Caller:** the launcher (deadline construction and every comparison) and the supervisor (enforcement boundaries); every deadline comparison in the review seam is replumbed to this domain.
- **Preconditions:** lease `not_before`/`expires_at`, command start/finish, and `exhausted_at` all persist in one boot-scoped monotonic domain; wall time is audit/correlation only. Clock choice (frozen): `CLOCK_BOOTTIME` via `time.clock_gettime(time.CLOCK_BOOTTIME)` — monotonic-including-suspend; Python's `time.monotonic()` excludes suspend on Linux and would undercharge a suspended host. Boot identity source: `/proc/sys/kernel/random/boot_id`.
- **Atomic persistence:** lease bounds persist in the claim transaction (with C5's `claimed_unarmed` persistence); command start/finish endpoints persist at observation.
- **Generation/watermark:** `exhausted_at` participates in the C3 finalization fence.
- **Idempotency:** replayed endpoints are idempotent; a previously trusted open prefix is never lowered by a later earlier endpoint — high-water retained, discrepancy recorded, hold for reconciliation.
- **Typed failures:** `cross_boot_comparison` (illegal comparisons cause a hold); `clock_reconciliation_required` (monotonic decrease/backward wall); `boot_discontinuity` — a boot-ID change produces a deterministic revoke/settle sweep: every prior-boot still-open action is revoked and settled as uncertain (never refunded, never relaunched), the attempt enters the typed reconciliation hold for new admissions, receipts record the discontinuity. The hold has an exit (sweep + explicit operator reconciliation per plan v2:282), not an infinite park; `insufficient_safe_window` interacts per C1.
- **Boundary semantics (normative, plan v2:147):** start at `not_before` allowed; command/final-identity finish strictly before `expires_at`; state-advancing finish strictly before `exhausted_at`; consumption equal to allowance is exhausted. Half-open.
- **Readback:** lease bounds and endpoints read back in the monotonic domain via the lease row/export; wall timestamps ride along as audit only. Legacy `deadline` values are read as wall history and never reinterpreted as monotonic bounds (migration, plan v3).

## C5 — Armed/released command-release token state machine (unit U5)

Plan v2 basis: lines 82 (durable fail-closed state machine; recovery rules), 84 (projection failure before/after release), 200 (verify/record/arm before release), plan v3 gated-launcher section (the release mechanism, frozen here by reference).

- **State machine (durable, fail-closed):** `reserved -> claimed_unarmed -> armed -> released -> revoked/settled`.
- **Caller:** the dispatcher's managed-claim path is the only actor that advances states; the gated launcher is the only executor.
- **Gated launcher mechanism (frozen design):**
  1. Dispatcher transaction: claim the run, persist action at `claimed_unarmed` with its lease bounds (no reviewer process exists). Then spawn the **launcher** (not the worker); the launcher calls `setsid()`, records its new pid + pgid; the dispatcher verifies `os.getpgid(launcher_pid) == launcher_pid` and start identity, and persists them together with state `armed` in one transaction. At `claimed_unarmed` the launcher is waiting, not executing; at `armed` there is a verified live group to record.
  2. Launcher gate: the launcher blocks reading a release token from a pipe/fd. The dispatcher writes the token **only after** the `armed` transaction commits and the audit projection is verified. On a token matching the persisted armed record, the launcher re-verifies its own pgid/identity and `exec()`s the reviewer command. Token check and exec are one uninterrupted step inside the launcher (no lock reacquisition, no scheduler yield between) — closing the check-to-exec TOCTOU window as far as a cooperative launcher can (see trust boundary above).
  3. Enforcement after release: arming persists an independently recoverable watchdog record (lease deadline, reserve, group identity) **and** a live supervisor timer. The watchdog record is what a restarted dispatcher reads to re-arm enforcement after a crash — a persisted record alone enforces nothing. At `min(lease_expires, exhausted_at) - reserve` the supervisor revokes admission, sends graceful termination to the verified group, escalates with SIGKILL inside the reserve, and records survivors. If the dispatcher dies after release, the launcher's own timeout `min(command_cap, lease_remaining, allowance_remaining)` bounds the command, and the next dispatcher recovery re-arms supervision from the persisted watchdog record.
  4. Orphan handling: a launcher orphaned between spawn and the `armed` commit is recovered by the next dispatcher pass (no persisted armed identity for the live launcher pid) — the launcher is killed (still blocked on the pipe, has executed nothing) and the action is failed/cancelled. A launcher whose armed record exists but whose token never arrived is treated the same way on recovery. Recovery from `reserved`/`claimed_unarmed` may cancel but can never launch (plan v2:82).
- **Preconditions:** only the original verified `released` state may execute; the command-release token is persisted separately from the run claim; enrollment is an explicit durable predicate (`enrolled` marker persisted at reservation, default absent/false for legacy rows), so "non-managed claim paths rejected for enrolled tasks" has a defined key.
- **Atomic persistence:** the `armed` transaction persists verified group identity + watchdog record + state; token issuance is separate and follows projection verification.
- **Generation/watermark:** n/a directly; the action participates in the C3 cohort watermark.
- **Idempotency:** token replay is idempotent (a second delivery of the same token does not re-launch or re-arm).
- **Typed failures:** `crash_before_release` (every boundary crash prevents command release); `orphaned_launcher` (recovered as above); `projection_failure_pre_release` (withholds the token, triggers native revocation, plan v2:84); `non_managed_claim_refused` (non-managed claim paths rejected for enrolled tasks); `revoked_settled` (recovery from `armed` revokes and settles — never relaunches). After release, a projection failure cannot override the native cap.
- **Readback:** action state, token-present flag (not the token), armed group identity, and watchdog record are visible via the supported export; the token itself is never part of any export.

---

## Baseline behavioral surface (25 re-derived probes, must keep passing)

The Phase A receipt at e2427daf passed all 25 behavioral probes. They are re-derived here with IDs and expected typed results; they form the baseline the U1-U5 build must not regress. Phase A evidence column cites the original probe ID.

| ID | Phase A evidence | Expected typed result |
|---|---|---|
| BASE-checkout-clean | P0-checkout | detached disposable worktree at the exact pinned commit, clean tree |
| BASE-module-roots | P0-roots | imported module roots resolve inside the candidate worktree |
| BASE-temp-board | P0-board | temporary board initialized with immutable `workflow_board.board_id` |
| BASE-claim-refused | P9-manual-claim-refused | claim with no reserved action refused before command execution |
| BASE-claim-ordering | P9-claim-ordering | reserved action -> transactional claim opens run and binds action to run |
| BASE-dup-claim-refused | P9-duplicate-claim-refused | second/foreign claim of the enrolled task refused while the run is open |
| BASE-stale-cas-refused | P9-stale-generation-refused | reserve with stale attempt version loses the CAS and is refused |
| BASE-identity-at-claim | P1-identity-at-claim | claimed event binds immutable board/task/run/lock identity in the claim txn |
| BASE-clock-checkpoint | P2-clock-checkpoint | persisted boot/monotonic/wall checkpoint paired in `review_clock` |
| BASE-discontinuity-hold | P2-discontinuity-hold | monotonic decrease/backward clock enters `clock_reconciliation_required` hold |
| BASE-gap-charges-zero | P3-gap-charges-zero | active time charges the open-run union; a 30s idle gap adds zero |
| BASE-equality-exhausts | P3-equality-exhausts | union consumption at exact allowance exhausts authority (held) |
| BASE-exhausted-no-admission | P3-exhausted-no-admission | exhausted lineage refuses new admission (claims fenced) |
| BASE-lease-bounds | P4-lease-bounds | per-action lease deadline persisted at claim; cap bounded by remaining active allowance |
| BASE-late-completion-denied | P4-late-completion-denied | completion at/after the action lease deadline cannot advance state |
| BASE-release-settlement | P5-release-settlement | release (review handoff) closes the action and the run row (authoritative close endpoint) |
| BASE-failure-settlement | P5-failure-settlement | crash/failed outcome settles the action failed without refund |
| BASE-recovery-reservation | P5-recovery-reservation | replacement after failure consumes the recovery reservation (new action id) |
| BASE-no-second-lease | P5-no-second-lease | primary repeat for the same action refused (no silent second lease) |
| BASE-supervisor-termination | P6-supervisor-termination | at union exhaustion the supervisor holds the lineage and cooperatively terminates the live worker process |
| BASE-revocation-deadline | P6-revocation-deadline | supervision deadline map zeroes revoked lineages |
| BASE-setsid-residual-recorded | P6-setsid-residual-recorded | setsid/double-fork escape observed and recorded as the accepted residual; local quiescence proof refused |
| BASE-readonly-export | P7-readonly-export | supported `hermes kanban show <task> --json` export works against the temporary board without direct DB access |
| BASE-state-machine | P8-state-machine | native lifecycle walks reserved -> running -> complete/settled through one managed claim path |
| BASE-unmanaged-write-refused | P8-unmanaged-write-refused | direct SQL status mutation of an enrolled task refused by the persistent guard trigger |

**Declared baseline changes (plan v3, authorized):** U1/U4 replace the wall-clock `time.time()+cap` deadline construction, so BASE-lease-bounds and BASE-late-completion-denied assertions change from wall-clock deadlines to monotonic-bound assertions (plan v2:145,147). U5 replaces `reserved -> running -> complete` and the single-tick spawn, so BASE-state-machine walks the new state machine `reserved -> claimed_unarmed -> armed -> released -> settled` (plan v2:82). No other review-seam behavior changes; the remaining baseline must pass unchanged.

**Named environmental exception:** `test_kanban_review_concurrency::test_recovery_and_claim_are_atomic_across_processes` failed in the Phase A baseline because the subprocess imports tower's installed runtime without inherited PYTHONPATH (passes with `PYTHONPATH=<candidate root>`). U0's probe freeze re-checks this case and either fixes its scaffolding (PYTHONPATH inheritance) or re-classifies it — it is not carried forward as an assumed-environmental waiver. Probe U0-concurrency-scaffolding below covers this.

## New per-capability probes (frozen before implementation)

Expected results are typed: a probe passes only by observing the named result; fail-closed refusals are themselves observable results.

### U0 (contract/suite sanity)
- **U0-contract-diff:** machine-diff contract probe IDs vs receipt probe IDs yields zero missing/extra — mechanical, no judgment.
- **U0-concurrency-scaffolding:** `test_recovery_and_claim_are_atomic_across_processes` re-run with explicit PYTHONPATH inheritance in its scaffolding; expected: passes under the fixed scaffolding, re-classified as a scaffolding fix rather than an environmental waiver. If it still fails after the scaffolding fix, it is a real defect and blocks.

### U1 — termination reserve
- **U1-config-validation:** a non-positive, non-integer, non-finite, or lease-exceeding `termination_reserve_seconds` at config load raises the typed `invalid_reserve_config` refusal; a valid default of 60 loads.
- **U1-insufficient-window:** remaining allowance <= reserve at issuance produces typed `insufficient_safe_window` — no lease, lineage parked, never rounded into extra execution.
- **U1-boundary-subtraction:** the launcher never starts a command whose timeout plus reserve reaches the action-lease or union-cap boundary (typed `reserve_exceeds_boundary` refusal attempt is observable).
- **U1-reserve-recorded-per-action:** the reserve actually used is recorded in the lease/receipt row at claim and survives a config change between actions (later action uses the new value; earlier receipt keeps the old).
- **U1-default-60:** absent config, the effective reserve is 60 and appears in the receipt as 60.

### U2 — process-group containment
- **U2-pgid-verified-at-arm:** the dispatcher verifies `getpgid(launcher_pid) == launcher_pid` plus start identity and persists them in the `armed` transaction; a mismatch is typed `containment_identity_mismatch` before any signal exists.
- **U2-child-grandchild:** a reviewer command that spawns a child that spawns a grandchild is terminated via the verified group (graceful then SIGKILL inside the reserve); all three processes observed gone; survivor enumeration returns empty.
- **U2-shell-wrapper:** a shell wrapper re-exec/child path is contained by the group; survivors enumerated as empty after escalation.
- **U2-ignored-sigterm:** a process ignoring SIGTERM is escalated with SIGKILL inside the reserve; observed gone by the reserve boundary.
- **U2-parent-exit:** a child that outlives its direct parent is still found by the `/proc` scan (pgid/session match) and terminated.
- **U2-supervisor-death-after-release:** dispatcher death after release leaves the launcher's own timeout bounding the command; the next dispatcher recovery re-arms supervision from the persisted watchdog record and settles/revoles as appropriate — no unbounded execution.
- **U2-pid-reuse-deterministic:** recorded (pid, start identity) cycled against a live group with a stale identity produces typed `containment_identity_mismatch` and NO signal — deterministic scaffolding, no scheduler race.
- **U2-stale-group-reuse-deterministic:** a recorded group whose leader exited, with the pgid potentially reused, is refused (`containment_identity_mismatch`); the `/proc` scan confirms the live group does not match the recorded leader start identity.
- **U2-self-group-refused:** an attempt to signal the supervisor's own pgid (or pgid <= 1) is refused — named probe, typed refusal, no signal sent.
- **U2-survivor-enumeration:** forced survivors (a process that escaped graceful termination) are enumerated by the `/proc` scan and recorded in the receipt before SIGKILL escalation.

### U3 — cohort export/fence
- **U3-export-fields:** a native cohort export carries board identity, authorization sequence, export generation, `finalized_through`, and completeness flag; all present and consistent with the claim history.
- **U3-export-ordering:** sequence allocation -> claim commit -> export snapshot -> finalization -> receipt eligibility is observed in that order; an export never contains an uncommitted claim.
- **U3-committed-not-exported:** a run committed before the fence but exported/closed late keeps finalization pending (typed `finalization_pending`) — state advancement is deferred, not skipped.
- **U3-concurrent-claim-vs-fence:** a claim racing finalization either commits before (appears in the generation) or fails after the fence (`cohort_claim_fenced`) — tested across separate OS processes.
- **U3-post-fence-claim-refused:** a claim after finalization is refused by the fence.
- **U3-finalization-vs-exhaustion:** finalization racing exhaustion fixes `exhausted_at` consistently; a receipt finishing before `exhausted_at` stays eligible; equality denies advancement.
- **U3-copied-db-rejected:** the board DB copied to a different canonical path/device with the same `board_id` is rejected (`board_identity_mismatch`).
- **U3-restore-needs-decision:** a legitimately restored board re-identifies only via a new operator decision record naming the restore; without it, path/device mismatch holds.

### U4 — boot-scoped monotonic bounds
- **U4-boottime-domain:** lease bounds/endpoints persist via `CLOCK_BOOTTIME` and boot identity from `/proc/sys/kernel/random/boot_id`; wall columns hold audit time only.
- **U4-half-open-start:** start exactly at `not_before` is allowed.
- **U4-half-open-finish:** finish exactly at `expires_at` is late (denied); strictly before is accepted.
- **U4-exhaustion-boundary:** state-advancing finish exactly at/after `exhausted_at` denied; strictly before accepted; equality of consumption and allowance exhausts.
- **U4-monotonic-decrease-hold:** observed monotonic decrease enters `clock_reconciliation_required`.
- **U4-boot-change-sweep:** a boot-ID change revokes and settles every prior-boot open action as uncertain (never refunded, never relaunched), records the discontinuity in receipts, and holds new admissions pending operator reconciliation — the hold has an exit.
- **U4-lease-vs-exhaustion-independence:** an unexpired lease cannot authorize work after exhaustion (lease validity and cumulative exhaustion are independent gates).

### U5 — armed/released token machine
- **U5-state-machine-boundaries:** the durable states walk `reserved -> claimed_unarmed -> armed -> released -> revoked/settled`; at `claimed_unarmed` no reviewer process exists; at `armed` a verified live group + watchdog record exist; the token is written only after the `armed` commit + projection verification.
- **U5-crash-at-every-transition:** injected crash at each transition boundary prevents command release (no state allows recovery-to-launch).
- **U5-token-replay-idempotent:** replaying the same release token does not re-launch or re-arm; a second launcher is never spawned for the same action.
- **U5-orphaned-launcher-recovery:** a launcher orphaned between spawn and `armed` commit (no persisted armed identity) is killed by the next dispatcher pass having executed nothing; action failed/cancelled, never launched.
- **U5-armed-no-token-recovery:** an armed record whose token never arrived is recovered the same way (killed, never launched).
- **U5-projection-failure-pre-release:** projection failure before release withholds the token and triggers native revocation.
- **U5-projection-failure-post-release:** projection failure after release cannot override the native cap — execution stays bounded, result permanently non-approvable.
- **U5-non-managed-claim-refused:** non-managed claim paths (manual/foreign/legacy) are rejected for enrolled tasks keyed on the durable `enrolled` marker; legacy rows default to unenrolled.
- **U5-recovery-from-armed-revokes:** dispatcher recovery finding `armed`-after-release revokes and settles rather than re-launching.

## Traceability matrix

plan-v2 line -> contract clause -> probe IDs -> unit.

| plan v2 ref | clause | probes | unit |
|---|---|---|---|
| :82 (state machine, recovery rules) | C5 | U5-state-machine-boundaries, U5-crash-at-every-transition, U5-orphaned-launcher-recovery, U5-recovery-from-armed-revokes, BASE-state-machine | U5 |
| :84 (projection before/after release) | C5 | U5-projection-failure-pre-release, U5-projection-failure-post-release | U5 |
| :86 (board identity anchor, exports carry identity/generation/completeness) | C3 | U3-export-fields, U3-copied-db-rejected, U3-restore-needs-decision | U3 |
| :145 (one monotonic domain, cross-boot illegal) | C4 | U4-boottime-domain, U4-monotonic-decrease-hold, U4-boot-change-sweep | U4 |
| :147 (boundary rules, reserve subtraction, timeout min) | C1+C4 | U4-half-open-start, U4-half-open-finish, U4-exhaustion-boundary, U1-boundary-subtraction | U1+U4 |
| :182 (watermark/sequence, fence, late sibling) | C3 | U3-export-ordering, U3-committed-not-exported, U3-concurrent-claim-vs-fence, U3-post-fence-claim-refused | U3 |
| :196 (operator reconciliation authority class) | C3 | U3-restore-needs-decision | U3 |
| :198 (integrity boundary, TOCTOU residual framing) | C2+C5 | U2-pgid-verified-at-arm, U5-token-replay-idempotent | U5+U2 |
| :200 (supervisor proof cases, setsid residual) | C2 | U2-child-grandchild, U2-shell-wrapper, U2-ignored-sigterm, U2-parent-exit, U2-supervisor-death-after-release, U2-pid-reuse-deterministic, U2-stale-group-reuse-deterministic, U2-self-group-refused, U2-survivor-enumeration, BASE-setsid-residual-recorded | U2 |
| :202 (finalization = native fence; four race cases) | C3 | U3-committed-not-exported, U3-concurrent-claim-vs-fence, U3-post-fence-claim-refused, U3-finalization-vs-exhaustion | U3 |
| :213 (timeout = min of caps) | C4+U2 | U2-supervisor-death-after-release | U4+U2 |
| :23 (setsid accepted residual) | C2 | BASE-setsid-residual-recorded | existing |
| :282 (operator reconciliation receipt exits holds) | C4 | U4-boot-change-sweep | U4 |
| :301 (macOS deferred, Linux/tower) | all | domain declaration on every probe | U0 |
| :472 (config source + validation, harness constants) | C1 | U1-config-validation, U1-default-60 | U1 |
| :474 (insufficient_safe_window) | C1 | U1-insufficient-window | U1 |
| plan v2:176 (reserve recorded per action, no retroactive config) | C1 | U1-reserve-recorded-per-action, U1-default-60 | U1 |
| plan v3 (enrolled marker durable) | C5 | U5-non-managed-claim-refused, BASE-unmanaged-write-refused | U5 |
| plan v3 (gated launcher orphans) | C5 | U5-orphaned-launcher-recovery, U5-armed-no-token-recovery | U5 |
| Phase A receipt (suite baseline environmental exception) | U0 | U0-concurrency-scaffolding | U0 |
| plan v3 (machine-checkable contract) | U0 | U0-contract-diff | U0 |

Baseline surface probes (BASE-*) map to the Phase A receipt probe IDs cited in the table above; the implementing unit for the baseline is "existing" (must keep passing), except where the declared baseline changes apply (BASE-lease-bounds, BASE-late-completion-denied -> U1+U4; BASE-state-machine -> U5).

## Sequencing note (frozen)

U0 -> U4 -> U1 -> (U2 + U5 jointly) -> U3 (may overlap U2+U5 once U4 lands) -> U6. U4's clock domain is the foundation every other unit writes deadlines in. Each unit is test-first and lands behind this contract; per-unit mini-receipts check against this contract as each unit lands (non-gating aid, plan v3).
