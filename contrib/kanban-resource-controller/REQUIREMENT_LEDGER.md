# External Kanban Build Admission Helper Implementation Contract

> **For Hermes:** Implement this contract task-by-task using strict serial TDD. Do not begin implementation while any prerequisite blocker marked `OPEN` remains unresolved.

**Goal:** Define the smallest external, unactivated helper that admits automatic Kanban build work only when the host is healthy, while preserving existing workers and leaving Desktop, cron, and messaging available.

**Architecture:** A standalone Python helper outside installed Hermes source samples Darwin host telemetry, reconciles canonical board/run state with exact live process identities, and invokes only supported Hermes CLI mutation paths. It owns the gateway dispatcher singleton lock for its active lifetime, performs at most one side-effecting command in each recovered eligibility window, and fails closed on ambiguity. Installation, service changes, activation, and live production dispatch are separate release-owner work.

**Tech stack:** Retained Hermes Python 3.11 environment and existing `psutil`; Python standard library; supported Hermes CLI; read-only SQLite URI transactions for validated inventory; per-user file locking and restrictive atomic JSON/log storage; serial `unittest` or the repository-approved serial runner.

---

## 1. Authority, provenance, and authorization

### 1.1 Binding sources

The sources are ordered. A lower source cannot override a higher one.

1. Live task `t_a4e29e97`, including its acceptance criteria and prohibitions.
2. `/Users/jhaynes/.hermes/plans/hermes-kanban-build-only-controller.md`, SHA-256 `1d76749432f8cbf69be78bdac99ae3470e6349f36ca0037da58cac3f80a62213`.
3. `/Users/jhaynes/.hermes/plans/hermes-resource-controller-build-handoff.md`, SHA-256 `a897e4fb59e929754026e106f7af2316f389161ee830d1bb2b48fa10a5bec65d`, only where consistent with sources 1-2.
4. `/Users/jhaynes/.hermes/plans/hermes-desktop-resource-containment.md`, SHA-256 `170ddca6104c228ccbdf86904913a2c62d37400ea74c301c63a8fb38fe51c0f0`, only for still-applicable drain and rollback constraints. Its broad automatic pause/resume proposal is superseded.
5. Retained Hermes source baseline `5910de20bc9839fdd36e791a9d72ba2c2e722f66` for executable CLI and schema contracts.

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
7. Exactly one side-effecting command against exactly one board in a recovered eligibility window: either one decomposition or one dispatch. Never both.
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

`hermes kanban --board <slug> dispatch --max 1 --failure-limit <configured-positive-int> --json`

At baseline, `hermes_cli/kanban_ops.py:60-108` loads the effective default-profile caps and passes them to `dispatch_once`. `hermes_cli/kanban_db_dispatch.py:1739-1775` defines `--max 1` as live per-board concurrency (already-running plus this tick), not a promise of exactly one start. The same source applies a host-wide `max_in_progress` across boards. The helper must never label `--max 1` a one-start flag.

Every supported dispatch tick performs reclaim/promotion before the spawn gate (`kanban_db_dispatch.py:1716-1736, 1853-1867`). Therefore no dispatch command may run during resource, ESTOP, manual, capacity, identity, compatibility, or uncertainty holds. The helper must reconcile before and after every command and must not retry an uncertain dispatch.

### 4.4 Whole-board selection limitation

The supported dispatch CLI accepts a board, not a task/stage selector. Within a board, baseline dispatch orders each lane by priority then creation time (`kanban_db_dispatch.py:1800-1806`), runs ready work before review while reserving one slot for review when possible (`1869-1933`), and can mutate maintenance state before selecting a worker. An external observer cannot atomically guarantee that the task it predicted remains the task selected.

This limitation is the central unresolved blocker in section 13. No implementation may silently substitute round-robin board selection for downstream-first resource priority.

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
- Explicit `--max 1` intentionally overrides `kanban.max_spawn` for this helper.
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

- Host admission ceiling: two exact matching live workers across all discovered boards and both ready/review lanes.
- Existing excess workers are allowed to finish; they cause a capacity hold.
- Per-profile ceiling: one live worker, retaining the configured defense-in-depth cap.
- Per-board automatic concurrency: one via `dispatch --max 1` plus pre-command canonical reconciliation.
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
| T-008 | Dispatch semantics | Real isolated supported CLI with harmless finite worker proves `--max 1` board concurrency, no second board worker while first lives, host cap 2, profile cap 1, and no `--max 0`. |
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

### B-001 — `OPEN`, prerequisite approval: downstream-first priority versus supported whole-board dispatch

Binding plan lines 18-22 require merge-conflict resolution before reviews, reviews before tests, and downstream completion before new builds. It explicitly says board round-robin is insufficient. The supported CLI selects a board, not a specific task/stage, and selection can change between observation and claim. The pinned candidate implements round-robin and explicitly admits any eligible task may be selected; it does not satisfy this requirement.

No-core options each change product policy: best-effort prediction accepts a race, refusing mixed boards risks starvation, and weakening/deferring downstream-first drops a requirement. A task-selective core seam was previously declined. Justin must explicitly choose an approved no-core tradeoff or amend the no-core boundary. Until then, implementation of active dispatch selection is blocked.

### B-002 — `OPEN`, prerequisite approval: priority classification, prerequisite work, yielding, and starvation

The binding plan leaves stage classification, resource reservation, prerequisite work needed to unblock higher-priority phases, safe yielding/preemption, and starvation handling as design questions. Workers may not be killed. There is no approved mapping from task metadata/status/assignee to merge/review/test/build stage and no approved starvation bound. These are consequential scheduler choices, not implementation details.

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
- Implementation/activation: intentionally not performed.
- Unresolved blockers: B-001 through B-006 remain explicit; B-001/B-002 block implementation of active selection, and B-003 through B-006 block activation/release.