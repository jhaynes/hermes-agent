# External Kanban Build Admission Helper Implementation Contract

> **For Hermes:** Implement this contract task-by-task using strict serial TDD. Do not begin implementation while any prerequisite blocker marked `OPEN` remains unresolved.

**Goal:** Define the smallest external, unactivated helper that admits automatic Kanban build work only when the host is healthy, while preserving existing workers and leaving Desktop, cron, and messaging available.

**Architecture:** A standalone Python helper outside installed Hermes source samples host telemetry, reconciles canonical board/run state with exact live process identities, and invokes only supported Hermes CLI mutation paths. It owns the gateway dispatcher singleton lock for its active lifetime, performs at most one mutating command in each recovered eligibility window (read-only dry runs excluded), and fails closed on ambiguity. Installation, service changes, activation, and live production dispatch are separate release-owner work.

**Tech stack:** Retained Hermes Python 3.11 environment and existing `psutil`; Python standard library; supported Hermes CLI; read-only SQLite URI transactions for validated inventory; per-user file locking and restrictive atomic JSON/log storage; serial `unittest` or the repository-approved serial runner.

---

## 1. Authority, provenance, and authorization

### 1.1 Binding sources

The sources are ordered. A lower source cannot override a higher one.

1. Live task `t_a4e29e97`, including its acceptance criteria and prohibitions.
2. `/Users/jhaynes/.hermes/plans/hermes-kanban-build-only-controller.md`, SHA-256 `b8bed3cb7745312aca32e6abc7751e7ee40af84ab7ffac56bb257374884b7905` (including the binding 2026-09-22 amendment at lines 59-79).
3. `/Users/jhaynes/.hermes/plans/hermes-resource-controller-build-handoff.md`, SHA-256 `a897e4fb59e929754026e106f7af2316f389161ee830d1bb2b48fa10a5bec65d`, only where consistent with sources 1-2.
4. `/Users/jhaynes/.hermes/plans/hermes-desktop-resource-containment.md`, SHA-256 `170ddca6104c228ccbdf86904913a2c62d37400ea74c301c63a8fb38fe51c0f0`, only for still-applicable drain and rollback constraints. Its broad automatic pause/resume proposal is superseded.
5. Retained Hermes source baseline `0e0a29ad315da6b6fd5b63e2903600af85e839e5` for executable CLI and schema contracts. This runtime compatibility pin is independent of the original comparison base recorded below.

The build-only plan explicitly says its reconciliation wins (lines 3-16), adds downstream-first priority (lines 18-22), selects the external-controller boundary (lines 24-25 and 36-43), separates cutover (lines 45-51), and defines validation/rollback (lines 53-57).

### 1.2 Authorization evidence

The task body initially required explicit release-owner authorization before bounded work. Justin directly requested `work kanban task t_a4e29e97` in the delivery that started this run. This is recorded as authorization for this finite requirement-reconciliation task through the existing builder capacity. The predecessor root also contains a `desktop` comment reading `Authorized!`; that earlier comment is corroborating launch-capacity evidence, not a product-policy amendment.

Authorization is limited to this contract document and isolated-worktree setup. It does not authorize implementation, configuration changes, installation, launchd changes, activation, production dispatch/decomposition, worker interruption, remote publication, or the old load-adaptive branch.

### 1.3 Isolated workspace handoff

- Worktree: `/Users/jhaynes/.hermes/hermes-agent/.worktrees/t_a4e29e97`
- Branch: `wt/t_a4e29e97`
- Original base: `5910de20bc9839fdd36e791a9d72ba2c2e722f66`
- Planned artifact root: `contrib/kanban-resource-controller/`
- This contract: `contrib/kanban-resource-controller/REQUIREMENT_LEDGER.md`
- Future deployment root, only after separate authorization/review: a restrictive per-user operations directory outside installed Hermes source, such as `$HERMES_HOME/operations/kanban-resource-controller/`
- Future runtime state: under that operations directory; future LaunchAgent plist: `~/Library/LaunchAgents/`; neither may be created during implementation.

The retained prior candidate at `/Users/jhaynes/.hermes/hermes-agent/.worktrees/t_206ec88f`, commit `a1ca24d59d87c17271195708cce6d2427d6da20b`, is evidence only. It is not the base of this contract and must not be activated or represented as satisfying downstream-first priority.

## 2. Scope classification before edits

### 2.1 `in_scope_required`

- This ledger, isolated-worktree handoff, source/CLI contract references, implementation boundaries, planned serial tests, and explicit blocker register.
- Future standalone files wholly under `contrib/kanban-resource-controller/`, but only after blockers are resolved and the implementation child is authorized.
- Tests/docs for the external artifact only.

### 2.2 `out_of_scope_followup`

- Smithers #2041; keep backlog-only and separate.
- Improving `kanban boards list` so its count path is genuinely read-only.
- General Hermes scheduler/task-selective dispatch features unless Justin later approves a separate Hermes-core amendment.
- Unrelated cleanup, package changes, profile redesign, notification migration, or old branch revival.

### 2.3 `prerequisite_needs_approval`

- Any policy that weakens, defers, or reinterprets downstream-first priority.
- Any Hermes-core/task-selective dispatch change.
- Any starvation-prone mixed-board hold policy, best-effort task prediction, or preemption/yield policy.
- Any change from configured stale-worker handling to CLI default behavior.
- Installation, service/config mutation, launchd experiment, production canary, or activation.

## 3. Non-negotiable implementation boundaries

1. No modifications to existing Hermes source, installed source, package manifests, dependencies, profiles, production databases, services, or configuration.
2. No automatic Hermes ESTOP creation, adoption, pause, resume, or clearing. The helper only reads ESTOP and maintains its own admission hold/status.
3. No worker kills, signals, reclaims, status relabeling, DB repair, fabricated ownership, or arbitrary process cleanup.
4. No private dispatcher imports. Mutations use supported CLI commands only. Read-only canonical inventory may use validated SQLite contracts because supported observation currently has side effects.
5. No recurring LLM inference in the control loop. A single supported decomposition may invoke the existing configured auxiliary model only when live `auto_decompose` is true and all other admission gates pass.
6. No `--all`, `dispatch --max 0`, standalone daemon, direct API dispatch, or production queue tests.
7. At most one mutating command against exactly one board in a recovered eligibility window: either one decomposition or one dispatch. Never both. Read-only dry runs do not count.
8. Run tests serially, one test process/native thread budget at a time, with fake or isolated homes/boards/processes and harmless finite workers.
9. Keep every operational artifact outside installed Hermes source. The linked worktree is staging only, not installation.
10. Preserve the old unreleased load-adaptive branch and all unique/dirty worktrees.

## 4. Supported CLI contract at the pinned baseline

### 4.1 Board enumeration

Semantic command: `hermes kanban boards list --json`.

At baseline, `hermes_cli/kanban_boards.py:51-57` lists unarchived boards and opens each board to calculate counts. That path can initialize/migrate/write and therefore must not run against production DBs as an allegedly read-only probe. The helper may execute the supported command only against metadata-only isolated replicas to verify output shape, then use validated read-only board paths for canonical inventory. Unknown slugs, duplicates, malformed JSON, command failure, timeout, or source/schema drift are compatibility holds.

### 4.2 Decomposition

Allowed form for one explicitly selected triage card:

`hermes kanban --board <slug> decompose <task-id> --author auto-decomposer --json`

`--all` is forbidden. The helper rereads the live boolean `kanban.auto_decompose` each decision. False means no decomposition. Decomposition consumes the sole side-effect window whether it succeeds, returns a no-op, fails, times out, or has an uncertain outcome. Output must identify the requested task and a confirmed success before the result is considered known. Existing auxiliary-model behavior is preserved; no new model loop is added.

### 4.3 Dispatch

Allowed form for one selected board:

`hermes kanban --board <slug> dispatch --max <computed-positive-m> --failure-limit <configured-positive-int> --json`

At baseline, `hermes_cli/kanban_ops.py:60-108` loads the effective default-profile caps and passes them to `dispatch_once`. `hermes_cli/kanban_db_dispatch.py:1739-1775` defines `--max` as live per-board concurrency (already-running plus this tick), not a new-start count. C′ therefore supplies `m = min(effective board cap, fenced selected-board database-running count + 1)` to dry and real dispatch; it never labels `--max` a one-start flag.

Every supported dispatch tick performs reclaim/promotion before the spawn gate (`kanban_db_dispatch.py:1716-1736, 1853-1867`). Therefore no dispatch command may run during resource, ESTOP, manual, capacity, identity, compatibility, or uncertainty holds. The helper must reconcile before and after every command and must not retry an uncertain dispatch.

### 4.4 Whole-board selection limitation and approved approximation

The supported dispatch CLI accepts a board, not a task/stage selector. Within a board, baseline dispatch orders each lane by priority then creation time (`kanban_db_dispatch.py:1800-1806`), runs ready work before review while reserving one slot for review when possible (`1869-1933`), and can mutate maintenance state before selecting a worker. An external observer cannot atomically guarantee that the task it predicted remains the task selected.

Justin resolved B-001/B-002 in the binding amendment dated 2026-09-22. Section 19 further amends dispatch to per-board supported `dispatch --dry-run --max <m>`, classifies predicted picks deterministically as merge-conflict > review > build, selects by stage with round-robin ties, and grants one aging admission after six passed healthy windows. The accepted dry-run-to-dispatch race never triggers a retry, second mutating command, or worker kill. Status calls the policy `best-effort downstream-first`, never guaranteed priority.

## 5. Configuration compatibility and refusal rules

Active mode must read effective default-profile configuration fresh and fail closed on malformed, unreadable, missing, type-invalid, or contract-incompatible values. Observation mode must not claim authority.

Required active-mode values or semantics:

- `kanban.dispatch_in_gateway` is explicitly `false`, and the old live gateway dispatcher is independently proven stopped before helper authority begins. Config alone is not proof.
- `kanban.max_in_progress` is exactly integer `2` for defense in depth.
- `kanban.max_in_progress_per_profile` is exactly integer `1`.
- `kanban.failure_limit` is a positive integer and is passed explicitly to dispatch.
- `kanban.auto_decompose` is a boolean reread each decision.
- `kanban.reconcile_orphans` is `true`, matching the supported CLI's hardwired/default behavior; `false` is an incompatibility hold because the CLI does not expose parity.
- `kanban.dispatch_stale_timeout_seconds` is `0`, or activation remains blocked. Baseline effective default is `14400`, but `_cmd_dispatch` does not pass it and `dispatch_once` defaults to `0`; no CLI flag provides parity.
- `kanban.review_dispatch`, `kanban.default_assignee`, and any `kanban.dispatch_profiles` restriction must be demonstrably honored by the supported CLI at the pinned source. Unknown/non-equivalent settings hold.
- `kanban.auto_decompose_per_tick` does not enlarge the helper's one-action window; the binding controller policy is stricter.
- Explicit computed `--max <m>` intentionally overrides `kanban.max_spawn` for this helper.
- Source commit/contract mismatch, unsupported schema, output-shape drift, missing required columns, too-large bounded inventory, or command parsing drift are persistent compatibility holds pending re-review.

The builder must not change incompatible production settings. Report the exact key/value mismatch for release-owner disposition.

## 6. Resource admission policy

Sampling interval target: 30 seconds using monotonic time for dwell/gap decisions.

### 6.1 Immediate hold conditions

Hold admission when any condition is true:

- load1 is greater than or equal to logical CPU core count;
- native macOS pressure is anything other than normal;
- available memory is less than 4 GiB;
- cumulative page-in increased since the prior valid sample;
- cumulative page-out increased since the prior valid sample;
- any required sensor is unknown, malformed, non-finite, inaccessible, or inconsistent;
- first sample, paging counter reset/decrease, restart, nonmonotonic time, or a sample gap too long to prove continuous health.

Historical swap occupancy alone is not an entry condition. Status must distinguish sustained paging from occupancy.

### 6.2 Recovery and cooldown

Admission becomes eligible only after at least 120 consecutive seconds in which every valid sample satisfies:

- load1 is less than or equal to `0.8 * logical cores`;
- available memory is at least 5 GiB;
- native pressure is normal;
- neither page-in nor page-out increases;
- no gap/reset/restart/unknown sample breaks continuity.

Recheck resource telemetry immediately before a command. Every possible side-effecting command resets the dwell/cooldown, including a confirmed no-op, failure, timeout, malformed result, or possible start. No more than one command can consume a window.

## 7. Singleton authority and locking

- Active mode owns the same gateway dispatcher singleton lock for its full lifetime.
- Lock acquisition is non-blocking and fail closed; contention means no authority/no side effects.
- The lock file descriptor is non-inheritable/CLOEXEC and child commands use closed inherited descriptors.
- Never unlink a held lock; doing so splits inode authority.
- Observation mode owns only a private state lock and never the dispatcher singleton.
- Active startup requires both config refusal (`dispatch_in_gateway=false`) and exclusive lock ownership. Either one alone is insufficient.
- After a command, retain singleton ownership while reconciling completion and descendants.

## 8. Counting, caps, process identity, and races

### 8.1 Admission caps

- Host admission ceiling: `admission.host_cap` exact matching live workers across all configured boards.
- Existing excess workers are allowed to finish; they cause a capacity hold.
- Per-profile ceiling: effective blanket/override cap; Hermes's global cap equals the largest effective value.
- Per-board automatic concurrency: configured blanket/override cap; C′ sends selected-board `m` plus pre-command canonical reconciliation.
- Terminal-card workers continue to consume capacity while their exact process identity lives.
- Manual CLI/API/agent starts and worker-internal fan-out are documented bypasses. This is admission control, not an atomic global semaphore.

### 8.2 Exact identity

- Parse canonical worker argv with the pinned Hermes argparse contract; never substring-match process command lines.
- Reconcile exact task id and board/profile markers with canonical `task_runs`, task state, spawned-event fingerprint, PID, OS creation time, and ancestry.
- Deduplicate by `(PID, creation time)`, not PID alone.
- Track known descendants and reparented processes conservatively; terminal task state does not erase a live worker.
- PID reuse, exec-changed argv, mismatched task/run/profile, stale living worker, inaccessible relevant same-user process, orphan claim, missing fingerprint, duplicate identity, or unrecognized candidate is an identity hold.
- Stale DB rows remain visible diagnostics; the helper does not repair them.

### 8.3 Race fences

- Snapshot canonical state and exact processes before selecting an action.
- Immediately before execution, reread config, ESTOP, manual hold, board/task state, run/process inventory, and telemetry.
- Abort/hold if the predicted action changed.
- Persist an fsynced pending journal before process launch; record child PID/creation identity as soon as known.
- Snapshot again after command completion and validate result against canonical runs/processes.
- A manual start between final check and CLI claim remains a documented race/bypass. Detect it post-command and hold; never claim a hard host cap.

## 9. Command supervision, ambiguity, timeouts, and persistent holds

- Bound read command output and duration (planned: 64 KiB and 15 seconds).
- Bound dispatch observation to 30 seconds and decomposition observation to 120 seconds.
- A deadline is not permission to kill the command or workers. Continue draining/reaping under singleton ownership.
- Nonzero exit, timeout, malformed/truncated/oversized output, parse failure, source/contract drift, crash in either launch window, or post-command disagreement creates `uncertain-outcome`.
- Persist pre/post reconciliation evidence. Do not retry, kill, auto-acknowledge, or clear uncertainty on restart.
- Transient telemetry errors may recover only through a fresh full 120-second dwell; command/identity/compatibility uncertainty requires explicit operator reconciliation.
- One failed or possible command consumes the window and resets cooldown.

## 10. Lifecycle, launchd descendants, state, and rollback

### 10.1 Runtime state

- Explicit absolute source, interpreter, home, user-home, and state paths; no hardcoded real home in tests.
- Config mode 0600, state directory mode 0700, owned regular files/non-symlink directories.
- Atomic restrictive status and journals; bounded rotated logs; bounded CLI output.
- Status distinguishes resource, capacity, ESTOP, manual, identity, compatibility, observation, cooldown, and uncertain-outcome holds.

### 10.2 Safe lifecycle

- `hold` prevents new admissions only.
- `resume` removes only the helper's own manual hold, never Hermes ESTOP, and resets recovery dwell.
- `stop` requests hold and waits for commands/workers/known descendants to drain naturally.
- Do not use `launchctl bootout`, `kickstart -k`, forced signals, or routine restart while descendants might belong to the helper's launchd coalition.
- `setsid`, reparenting to PID 1, or detached status does not prove survival from launchd coalition teardown.
- Unknown descendant/coalition identity fails safe and blocks administrative unload.
- Actual launchd topology and harmless-child behavior require a separately authorized release-owner canary before activation.

### 10.3 Rollback

- Hold admissions first; preserve journals/status.
- Drain naturally and verify service exit, canonical runs, exact process identities, and coalition state before unload.
- Keep embedded gateway dispatch disabled if controller failure/uncertainty remains; an idle queue is safer than dual/uncontrolled dispatch.
- Restore the previous reviewed artifact/plist only after safe drain.
- If returning dispatch to the gateway, restore prior exact config values, including prior unset values, through supported commands and restart only after affected work drains.
- Never clear ESTOP, delete uncertain journals, remove state/worktrees, or force worker termination as rollback.

## 11. Legacy unowned notification subscriptions

Baseline gateway notifier includes subscriptions with null/blank `notifier_profile` only when the gateway owns the dispatcher lock (`gateway/kanban_watchers_notifier.py:179` and `kanban_db_notify.py:143-176`). Moving dispatcher-lock ownership to an external helper can therefore stop legacy unowned delivery even if `notify_in_gateway` remains enabled.

Historical read-only queries found zero unowned subscriptions in four discovered board databases. This is historical evidence only, not a guarantee about future delivery or a migration authorization.

Active preflight must reread every discovered board in read-only mode. Any null/blank `notifier_profile` is a compatibility hold unless the release owner separately proves equivalent routing or authorizes a migration. The helper must not fabricate or rewrite notification ownership.

## 12. Planned serial test contract

Each behavior is a vertical RED→GREEN slice after blockers are resolved. Keep receipts with exact command, source/base SHA, environment isolation, expected failing reason, passing result, duration, and snapshot hash.

| ID | Requirement | Planned test/evidence |
|---|---|---|
| T-001 | Threshold boundaries | Table tests at load `<`, `=`, `>` cores; available memory immediately below/at 4 GiB and 5 GiB; pressure normal/warning/critical/unknown. |
| T-002 | Paging both directions | Controlled cumulative page-in/page-out increase, unchanged counters, resets/decreases, first sample, sustained paging status. |
| T-003 | Recovery dwell | Fake monotonic clock proves no admission before full 120 seconds, exact boundary eligibility, gap/nonmonotonic/restart reset, and fresh dwell after command. |
| T-004 | Unknown telemetry | Missing/malformed/non-finite sensors fail closed; transient recovery requires new full dwell. |
| T-005 | Singleton lock | Two isolated processes contend; exactly one owns authority; CLOEXEC/non-inheritance verified; observation does not take dispatcher lock; recovery only after real release. |
| T-006 | Config refusal | Each required key wrong/missing/type-invalid independently; malformed YAML; source baseline drift; schema/output mismatch; dispatch-stale nonzero; embedded dispatcher true. |
| T-007 | Supported CLI decomposition | Isolated board invokes exactly one task id with author/json; `--all` absent; live auto-decompose false prevents call; result task mismatch holds. |
| T-008 | Dispatch semantics | Real isolated supported CLI with harmless finite worker proves successive explicit computed `m`, configured non-default host/profile/board boundaries, selected-board database-running counts, and no `--max 0`. |
| T-009 | One command/window | Eligible window chooses decomposition OR dispatch; success/no-op/nonzero/timeout/parse failure each consumes window; no same-window retry. |
| T-010 | Round selection safety | No round-robin acceptance test may claim downstream-first. Add tests only after blocker B-001's approved mechanism is recorded; verify merge/review/test/build contention and starvation policy. |
| T-011 | Pre-command races | Inject config, ESTOP, manual hold, task state, process, and telemetry changes between selection and execute; each prevents command. |
| T-012 | Post-command ambiguity | Spawned task differs, process fingerprint missing/reused, extra manual worker appears, or output disagrees with canonical state; uncertainty persists across restart. |
| T-013 | Process reconciliation | Exact argv parsing; PID+creation dedupe; task/run/profile mismatch; terminal-card live worker; stale row; inaccessible process; reparented descendants; exec-changed argv. |
| T-014 | Timeout supervision | Real finite child exceeds read/dispatch/decompose deadline, survives deadline, remains tracked/drained, receives no signal, and blocks repeat. |
| T-015 | ESTOP behavior | Existing ESTOP prevents all side-effect commands and remains byte-for-byte unchanged; helper never creates/removes it. |
| T-016 | Board enumeration | Supported boards-list JSON contract runs only on metadata replicas; production fixture DB hash/WAL state remains unchanged; malformed/duplicate/archived board behavior is explicit. |
| T-017 | Read-only inventory | SQLite URI `mode=ro` + `query_only`; required schema/row bounds; task/run/event coherence; no repair/write; query deadline. |
| T-018 | Unowned subscriptions | Zero permits preflight but is not cached; null/blank on any board holds; named profile rows remain untouched. |
| T-019 | Restrictive storage | 0600/0700, symlink/ownership refusal, atomic fsync/replace, bounded log rotation/output, crash-window journals. |
| T-020 | Lifecycle descendants | Harmless synthetic command/worker descendant blocks natural stop; survives until its own release condition; unknown descendant refuses stop/unload claim. No launchctl in builder tests. |
| T-021 | Rollback | State-machine test proves hold→drain→offline ordering, exact prior config receipt, no ESTOP clear, no dual dispatcher, and uncertainty leaves queue idle. |
| T-022 | Real host evidence | Read-only Darwin telemetry samples only; report observed hold/health honestly. Fixture recovery is not claimed as live recovery. |
| T-023 | No production mutation | Before/after hashes/metadata for production config, ESTOP, DBs, plist/services, installed source; tests use temporary HOME/HERMES_HOME/KANBAN_HOME and stub workers only. |
| T-024 | Source/static boundaries | Inventory confirms only external artifact files changed; no private dispatcher imports, shell execution, process kill/terminate, `--all`, pause/resume, DB repair, or hardcoded home. |

Future quality gates, once implementation is authorized: targeted serial artifact runner; one-process `compileall -j 1` for artifact only; `plutil -lint` template; `git diff --check`; frozen commit SHA plus cumulative diff from the original base. Do not run the full repository test suite or parallel tests under this resource-constrained contract unless separately approved.

## 13. Unresolved blocker register

### B-001 — `RESOLVED FOR IMPLEMENTATION`, approved best-effort whole-board prediction

Justin's 2026-09-22 binding amendment selects per-board supported dry-run prediction and explicitly accepts the bounded dry-run-to-dispatch race. Divergence is a `priority_miss` diagnostic and never authorizes retry, a second command, or a worker kill.

### B-002 — `RESOLVED FOR IMPLEMENTATION`, deterministic stages and bounded aging

The same amendment defines merge-conflict (`builder*` plus title regex `conflict|merge|rebase`) > review (`review*`) > build, round-robin ties, and one aging admission after six consecutive passed healthy windows. No yielding or preemption is authorized.

### B-003 — `OPEN`, activation prerequisite: stale-timeout CLI parity

Effective default `dispatch_stale_timeout_seconds` is 14400, but supported `kanban dispatch` does not forward it and `dispatch_once` defaults to zero. Active helper must refuse nonzero configuration. Changing production policy to zero or adding a supported capability requires separate approval.

### B-004 — `OPEN`, activation prerequisite: launchd coalition behavior

The builder phase forbids launchd changes. Synthetic process ancestry can test drain logic but cannot prove arbitrary descendants survive launchd bootout. Release owner must validate the actual topology with harmless children after code review and before activation; no unsafe-unload claim is allowed meanwhile.

### B-005 — `OPEN`, rollout prerequisite: live unowned-subscription recheck

Four board databases historically had zero unowned subscriptions. A future preflight must recheck all discovered boards. Any unowned row blocks activation pending separately approved routing proof or migration.

### B-006 — `OPEN`, release prerequisite: exact snapshot review and scope veto

Every implementation review round must include the dedicated `reviewscope` reviewer under the cross-company model-maker rule, with exact SHA, original contract sources/base, cumulative diff, mapping, classifications, and real evidence. An unresolved scope veto blocks delivery. No review or activation has occurred for this contract.

## 14. Implementation sequence after blocker resolution

1. Record Justin's explicit disposition for B-001/B-002 as an amendment on the live task and in this ledger; do not infer it from generic authorization.
2. Revalidate hashes, baseline, worktree cleanliness, and current config/schema/CLI contract read-only.
3. Write one failing serial policy test, verify RED, implement only that policy slice, verify GREEN.
4. Repeat vertical TDD for restrictive storage and singleton locking.
5. Repeat for bounded command supervision and persistent uncertainty.
6. Repeat for exact argv/process/canonical inventory and caps.
7. Repeat for source/config/schema/notifier compatibility refusal.
8. Implement the approved downstream-priority mechanism with its contention/starvation tests first.
9. Add supported CLI adapters and isolated real-CLI canary; never touch production queue.
10. Add lifecycle/rollback state machine, docs, and uninstalled LaunchAgent template.
11. Run targeted serial gates, freeze a commit, and hand the exact snapshot to implementation verification/reviews.
12. Leave installation, configuration/service handoff, launchd canary, and activation to the separately authorized release workflow.

## 15. Acceptance status for `t_a4e29e97`

- Concrete implementation/test contract: complete in this ledger.
- Binding-plan references and reconciliation: complete.
- Implementation boundaries and supported CLI contract: complete.
- Incompatible-config refusal rules: complete.
- Thresholds/recovery, lock, caps, command windows, races, timeouts, cooldowns, identity, descendants, ambiguity, lifecycle, rollback, and legacy notification behavior: explicitly enumerated.
- Historical zero-unowned evidence: recorded as historical only.
- Smithers #2041: separate/backlog-only.
- Isolated workspace handoff: established.
- Authorization evidence: recorded and bounded.
- Implementation is now authorized only in the external staging artifact; installation/activation remain forbidden.
- Unresolved blockers: B-001/B-002 are resolved by Justin's 2026-09-22 binding amendment. B-003 through B-006 remain activation/release prerequisites and do not authorize production changes in this implementation task.

## 16. Live CLI contract amendment (2026-09-24)

The 72f5a82e snapshot pinned an 11-key `kanban dispatch --json` shape that predates the retained source (`ec64ec0d24c`, 2026-09-15, added `reaped_terminal_workers`, `respawn_guarded`, `rate_limited`, `skipped_locked`, `memory_pressure`). Against the live CLI, every prediction failed closed with `dispatch output contract mismatch` and every real dispatch would have been recorded as persistent uncertainty, so the helper could never admit work. The fixtures, not the product, carried the stale shape.

Amendment, scope limited to the CLI contract: the exact key set is the live 16-key shape; field types are checked; `reaped_terminal_workers` and `rate_limited` count as maintenance (refuse prediction); `skipped_locked=true` refuses prediction and makes a real dispatch uncertain (dual-dispatcher evidence); any non-null `memory_pressure` refuses prediction (the helper's own Darwin gate stays authoritative). `respawn_guarded` rows are informational. Runtime source pin moves to the live retained HEAD `0e0a29ad315da6b6fd5b63e2903600af85e839e5` (ee5ee84a345 plus the local search_files rg cherry-pick; no kanban files differ). No policy, threshold, cap, lock, lifecycle or rollback behavior changes.

## 17. Cross-platform memory-telemetry amendment (2026-09-24), supersedes 6.1/6.2

Binding source: `~/.hermes/plans/kanban-controller-swap-telemetry-fix.md`, MoA v2 reconciliation, and owner decisions D1/D2/D3.

**The bug this amendment fixes:** sections 6.1/6.2 (and the original T-002 acceptance) described the Darwin sampler reading `psutil.swap_memory().sin`/`.sout` for "paging" counters. On macOS, `psutil` populates those fields from `vm_stat`'s **Pageins/Pageouts** — ordinary file-backed page traffic, not swap activity. The controller therefore held admission on routine file I/O and, on this Mac, swap-ins alone were nonzero in 9 of 10 idle samples — the D1 alternative "B: swap-ins hold above a named threshold" and "C: both strict" were rejected specifically because they starve on this evidence.

**Amendment (D1 = A, adopted):** only real swap-**out** growth gates admission. Swap-in growth is reported (raw value and non-negative delta, `null` on baseline/reset) but never holds or resets recovery. This applies identically on both OSes.

**Fix (root cause, both OSes):** a new `resource_controller/telemetry/` package replaces the psutil-based Darwin sampler:
- Darwin parses `/usr/bin/vm_stat`'s real `Swapins:`/`Swapouts:` lines directly (subprocess only, `psutil.swap_memory` never called); pressure is unchanged (`kern.memorystatus_vm_pressure_level` via `sysctl`).
- Linux reads `/proc/vmstat` (`pswpin`/`pswpout` × `SC_PAGE_SIZE`), `/proc/meminfo` (`MemAvailable`), and `/proc/pressure/memory` (PSI) directly, no subprocesses, each capped at 64 KiB.
- D2 (Linux PSI mapping defaults, adopted): warning at `some avg10 >= 10.0`, critical at `full avg10 > 0.0`; overridable per host via an optional `controller.json` `telemetry.linux_psi.{some_avg10_warning, full_avg10_critical}` section, validated by `RuntimeSpec` (absent section => defaults; the Mac deployment's `controller.json` is unaffected and stays byte-identical; Hermes `config.yaml` never carries these keys). The spec is read once at process start; threshold changes require a controller restart.
- D3 (Linux without PSI, adopted, fail closed): `ENOENT`/`EOPNOTSUPP` reading `/proc/pressure/memory` raises `TelemetryError(error_code="psi-unavailable")`, surfaced as `reason: telemetry-error`. There is no degraded swap-only fallback; the runbook instructs enabling PSI instead.
- Any telemetry failure (unreadable/malformed/oversized counters, unsupported platform, PSI unavailable) is caught specifically as `TelemetryError` in the run loop, writing `reason: telemetry-error` with a machine-readable `error_code` and a sanitized <=300-char single-line `diagnostic`, instead of the generic `persistent-operator-hold`. `ControllerEngine.invalidate()` drops both the policy baseline/dwell and the status-delta baseline, so stale counters are never reused: the next good sample is `swap-baseline` with null deltas, and deltas are also null on `sample-gap`, `swap-reset`, `nonmonotonic-time` and `unknown-telemetry` samples.

**Schema bump:** `status.json`/`check` output move to `schema_version: 2`, renaming `page_in`/`page_out` to `swap_in_bytes`/`swap_out_bytes` and adding `counter_page_size_bytes`, `units`, `source`, `pressure_detail`, `swap_in_delta_bytes`, `swap_out_delta_bytes`, `sample_wall_time`, `interval_seconds`, `telemetry_age_seconds`. See `RUNBOOK.md`'s schema 1 -> schema 2 mapping table. No consumers exist for the old field names, so no deprecated alias is kept — see the "Deprecated aliases (rejected)" note in the binding plan.

**No changes** to load/memory/dwell thresholds (load 0.8×cores / 1.0×cores, 4 GiB / 5 GiB, 120s dwell, 35s sample-gap cap), caps (host 2, per-profile 1, board `--max 1`), lock, lifecycle, rollback, or the CLI dispatch contract from section 16. This amendment is implementation/telemetry-correctness only.

**Scope:** `contrib/kanban-resource-controller/` only, in the linked worktree `~/.hermes/hermes-agent/.worktrees/controller-memory-telemetry` (branch `ops/controller-memory-telemetry`, base `fbb969fdba7603556c01bf61af5964bbaa493576`). Superseded text in 6.1/6.2 (Darwin-only `psutil` paging counters, `page_in`/`page_out` field names, `pressure_level`-only status) stays for historical record; this section is authoritative.

## 18. Worker start-fingerprint identity amendment (2026-09-24), supersedes §8.2's PID-only dedup wording

**Binding source:** `~/.hermes/plans/kanban-controller-worker-fingerprint-fix.md`, "MoA v1 reconciliation (binding)" section, which overrides that plan's earlier draft sections. Incident evidence: `~/.hermes/plans/receipts/kanban-controller-incident-t_c9f3e704-20260924.txt` (golden value `'|179027411681'`, psutil `create_time` 1790274116.812699, confirmed identical to pinned Hermes `_process_fingerprint` output for that process).

**The bug:** `board_inventory.py::_canonical_run` called `int(row['worker_started_at'])` on a column that pinned Hermes (`hermes_cli/kanban_db_dispatch.py::_process_fingerprint`/`_set_worker_pid`) never writes as an integer. The live writer emits either the literal string `"unverified"` or a composite `f"{current_instantiation_epoch()}|{start}"` — `int()` on the composite raises a bare `ValueError`, and `int()` on `"unverified"` does too, both uncaught. `inventory.py::reconcile_workers` separately compared `int(process.created_at)` (psutil seconds, a float) against the stored value with no unit conversion and no epoch — a unit and identity-drift bug independent of the crash.

**Root-cause provenance (pinned Hermes `0e0a29ad315da6b6fd5b63e2903600af85e839e5`, read-only, ported not imported):**
- `hermes_cli/kanban_db_dispatch.py` `_process_fingerprint` (~L367): writes the composite string or `"unverified"`.
- `hermes_cli/kanban_db_dispatch.py` `_pid_recycled` (~L398): exact string comparison, no tolerance.
- `hermes_cli/kanban_db_dispatch.py` `_set_worker_pid` (~L1459): the sole writer of `tasks.worker_started_at`/`task_runs.worker_started_at`.
- `gateway/drain_control.py` `current_instantiation_epoch` (~L41): `f"{boot_id}:{pid1_start}"`, `""` if both reads fail, partial string if only one succeeds.
- `gateway/status.py` `_get_process_start_time` (~L458): Linux reads `/proc/<pid>/stat` via `.split()[21]` (field 22, buggy if `comm` has spaces — ported verbatim for parity with the writer, not "fixed"); Darwin uses `psutil.Process(pid).create_time()` scaled to centiseconds (`* 100`, rounded).

**Decision (composite-only, no legacy int/NULL support, no drift tolerance):** every live writer produces either the composite or `"unverified"` — never an integer, never NULL. `resource_controller/worker_identity.py` is the new single owner of this contract: strict-length-check-then-regex parse (`^[^|]{0,200}\|[1-9][0-9]{0,19}$`, no leading zeros, canonical decimal), and match is **exact string equality** of `f"{epoch}|{start}"` against the stored raw value — not an int cast, not a tolerance window. Every identity problem (malformed, unverified, mismatch, unavailable, unsupported-platform, unstable, conflict) raises an `IdentityHold` subclass; callers never see a bare `ValueError`.

**New/changed files:**
- `resource_controller/worker_identity.py` (new) — the contract: `StoredFingerprint`, `parse_stored_fingerprint`, `current_instantiation_epoch`, `current_process_start`/`current_fingerprint`, `matches`/`require_match`/`require_consistent`, and the `IdentityHold` subclasses.
- `resource_controller/board_inventory.py` — SQL extended to select `r.worker_pid`/`r.worker_started_at` (task_runs side) alongside the `tasks` side; `_canonical_run` calls `require_consistent` so a tasks/task_runs disagreement is `identity-conflict`, not silently taking one side.
- `resource_controller/inventory.py` — `CanonicalRun.worker_started_at: int` → `CanonicalRun.worker_fingerprint: StoredFingerprint`; `ProcessSnapshot` gains `start_fingerprint: int | None`; `reconcile_workers` takes `epoch: str` and uses `matches()` instead of raw int/tuple comparison.
- `resource_controller/processes.py` — reads the process start fingerprint twice (before and after argv/env collection), raising `IdentityUnstable` on disagreement (one recapture budget, per MoA v1 decision 4).
- `resource_controller/runtime.py` — computes `current_instantiation_epoch()` once per `capture()` and threads it through, so a single snapshot uses one consistent epoch.
- `resource_controller/main.py` `_check` — `check` output gains an `identity` block (contract description, pinned commit, workers verified) per plan acceptance item 9.

**Mandatory contract gate (`scripts/contract_gate.py` + `tests/test_contract_gate.py`):** end-to-end, real child process, fingerprint written by the **pinned Hermes** `_process_fingerprint` (subprocess call into `HERMES_SOURCE_ROOT`, never imported into the controller's own process), read back by the controller's `RuntimeWorld.capture()` against a realistic sqlite board built with pinned Hermes's own `tasks`/`task_runs` DDL. The gate is mandatory: it **fails** (exit 1, not skip) if `HERMES_SOURCE_ROOT` is unset, and asserts `git -C $HERMES_SOURCE_ROOT rev-parse HEAD == 0e0a29ad315da6b6fd5b63e2903600af85e839e5` before trusting anything it imports. The ordinary hermetic `unittest discover -s tests` run skips these 2 tests cleanly when the env var is absent — normal dev/CI is not blocked by the pinned-source requirement.

**Mutation testing (9 hand-crafted mutants, all killed):** accept `"unverified"`; ignore epoch half; seconds instead of centiseconds; ±200 tolerance window; `int()` on the composite; accept a second `|`; accept leading zeros in `start`; drop the tasks-vs-task_runs conflict check; Linux start time from `psutil` instead of `/proc/<pid>/stat` `.split()[21]`. Each was applied to a disposable copy of the package and the full test suite re-run; each caused a failure.

**Deviation from plan (reported, not silently dropped):** the plan asks the Linux contract-gate docker run to mount the pinned Hermes source read-only "if feasible." The pinned venv has 282 third-party packages; installing that dependency graph into a `python:3.12-slim` container is out of scope for this task and was not attempted beyond confirming `psutil`/`pyyaml` alone are insufficient to import `hermes_cli.kanban_db_dispatch`. The Linux gate that *was* run in docker is the hermetic `unittest discover` (169 tests, 2 skips, clean) — the pinned-source contract gate itself ran only on macOS, where the pinned venv already exists.

No change to thresholds, caps, telemetry, lock, or lifecycle behavior from §17 or earlier sections; no CLI dispatch/decompose contract change from §16.

**Review round 1 follow-up (2026-09-24, desktop, after deleg_64b51783):** reviewtests found that some mutants survived and reviewsystem found a missing recovery procedure. Fixed as follows:
- `processes.py`: the two start reads now bracket the actual environment read (`_stable_identity`). One recapture re-reads both the start and the environment, and a second disagreement raises `IdentityUnstable`. Before this fix, both reads happened back-to-back after argv parsing and before `environ()`, so they bracketed nothing. `scan_worker_processes` gained injectable `process_iter`/`start_fn` for hermetic tests (`tests/test_processes.py`). The unused `capture_stable_fingerprint`/`fingerprints_agree` helpers were removed.
- `tests/test_contract_gate_script.py` unit-tests the gate's own control logic: missing env → 1, unreadable or wrong HEAD → 1 before any test runs, skipped/failed/empty → 1, and pinned + passing → 0.
- `tests/test_board_inventory.py`: a NULL `worker_started_at` with a live `worker_pid` on either the tasks or the runs side holds with "incomplete active identity".
- `tests/test_worker_identity.py`: an oversized value never reaches the regex; Darwin exact-half centisecond values round the same way as the pinned expression.
- RUNBOOK: pending-journal recovery after `uncertain-outcome` (plan decision 8), with still-running/finished/other branches, byte copy plus sha256, and an atomic reconciled rewrite through `SecureStateStore.write_json`.
- Newly killed mutants (9): NULL bypass; skipping the second read; no `IdentityUnstable` raise; recapture that does not re-collect; environment read outside the bracket; gate without the HEAD check; gate returning 0 on missing env; gate accepting skips; no pre-regex length check.
- Not fixed in the package: the gate is mandatory by procedure (RUNBOOK install step and the release checklist on card t_0aa43069), not by CI. There is no CI job for this contrib package.

## 19. Configurable admission caps and C′ dispatch amendment (2026-09-24)

Binding sources: approved plan `/Users/jhaynes/.hermes/plans/kanban-controller-configurable-caps.md`, SHA-256 `3edce15d0305d4e067a028a06aab5f7d0bc1346b8f66b41b7fc04b116cd1cf3f`, and binding addendum `/Users/jhaynes/.hermes/plans/kanban-controller-configurable-caps-addendum-1.md`, SHA-256 `92454021d4d5ae73dabc02d9562dc62a14b4fa7e92e928f37ff5f9a7ae200fbf`. This section supersedes fixed-cap and literal `--max 1` statements in §§4.3, 5, 8.1, 12/T-008–T-012, and §17's “no cap change” sentence. It does not change telemetry, thresholds, dwell duration, lock, lifecycle, or worker identity.

- `controller.json.admission` is the sole controller policy source: positive-integer `host_cap`, `profile_cap`, `profile_overrides`, `board_cap`, and `board_overrides`. The absent-section 2/1/1 values are declared once in `spec.py`. Effective profile caps cannot exceed host; board override names must be configured boards; profile override names must be members of a fresh explicit, nonempty, duplicate-free `kanban.dispatch_profiles` registry.
- Hermes remains defense in depth: raw `kanban.max_in_progress` must equal controller host; raw `kanban.max_in_progress_per_profile` must equal the largest effective controller profile cap. Mismatch is visible and nonzero in `check`; the helper never rewrites config.
- Each board snapshot records `hermes_db_running_count` (`tasks.status='running'`) separately from `controller_reconciled_live_count` (all exact living workers, including terminal-card processes). Both are fingerprinted. Admission uses the latter. C′ computes `m = min(effective board cap, hermes_db_running_count + 1)` from the selected final fenced board and sends the same explicit positive `m` to dry run and real dispatch. At cap it holds and never emits `--max 0`.
- The eligibility-window invariant is **at most one mutating dispatch command** (or one decomposition). Read-only dry runs do not count. A valid dry run has zero or one row; more than one is `precommand-race` with no real command. Every row belongs to the selected board's fenced candidate set and an eligible profile.
- Normal operation leaves one Hermes board slot. A completion race may let the one mutating command start extras up to Hermes's host/global-profile limits and board `m`. Those are the only preventive bounds. Stricter controller-only limits are verified afterward, never described as Hermes-enforced; violation is persistent uncertainty, workers continue, and nothing is killed.
- Post-command evidence compares full `(task id, run id, worker identity)` pre/post sets. Ordered CLI rows must exactly equal new identities; the first row retains prediction semantics; finished baseline workers are allowed; unrelated/manual arrivals, missing identities, duplicates, malformed rows, more than `m`, or any post-cap violation stay uncertain.
- New writers dual-write scalar `actual_task_id` (first id or null), ordered `actual_task_ids`, and `extra_starts`; status schema remains 2 and fields are additive. Old scalar and new dual-written reconciled journals are nonblocking; all pending/uncertain forms block. Rollback drains, requires reconciled pending state, checkpoints state, archives/removes status, and restores the prior controller JSON before the old binary.
- A valid multi-start consumes the command window before launch and restarts the full recovery dwell. `status.json` records the bounded receipt; cap uncertainty records reason/name/count/cap.
- Rollout preflight inspects `kanban.dispatch_profiles` for absent/null/empty before installing the binary. Cap changes use hold → stop → edit controller and matching Hermes values → check → restart held → resume.

TDD receipts: parser/config/inventory/database-count/explicit-`m` tests were observed failing against base `7771e842390d627baa99e1ceee026f2ef20512cd` before their implementation slices (missing admission field, rejected admission key, missing caps argument/helper/count, and fixed `--max 1`). The completed suite adds non-default host/profile/board boundaries, final-fence and multi-start identity reconciliation, profile/host post-cap uncertainty, dual-written rollback compatibility, check-output mismatch/counts, and one-mutating-command/recovery-dwell evidence. Mutation receipts and final platform counts are recorded in `REQUIREMENT_MATRIX.md` and `PACKAGE_MANIFEST.md` at freeze time.

