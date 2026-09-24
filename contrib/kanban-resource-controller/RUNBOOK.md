# Operational runbook — unactivated artifact

## Status and authority boundary

This document is a future release-owner procedure. The build, verification, and packaging tasks did **not** install the helper, change Hermes configuration, alter a service, load a LaunchAgent, dispatch or decompose production work, clear or create Hermes ESTOP, remove a package, activate the unreleased load-adaptive branch, or run a production canary.

Reading or reviewing this runbook is not authorization to execute it. The following require separate, explicit release-owner authorization at the time of execution:

- production read-only discovery and compatibility receipts;
- the isolated Darwin launchd coalition canary;
- any production config, installation, service, or package change;
- any live zero-start check against production state;
- activation, production dispatch/decomposition, or ESTOP transition;
- rollback that changes production config or services.

Smithers #2041 is separate, backlog-only work and is not part of this artifact or runbook.

## Frozen inputs and prerequisites

Before any operational step:

1. Obtain the packaged archive, release receipt, and manifest. Verify the archive SHA-256, frozen Git commit/tree, original comparison base `5910de20bc9839fdd36e791a9d72ba2c2e722f66`, reviewed retained Hermes source baseline `0e0a29ad315da6b6fd5b63e2903600af85e839e5`, and every packaged-file SHA-256 before extracting or running anything. The comparison base identifies the cumulative external-artifact diff; the source baseline independently pins the installed CLI/schema contract.
2. Confirm exact-snapshot quality review and dedicated cross-company `reviewscope` approval with no unresolved scope veto. Review must cover the cumulative diff, this runbook, `PACKAGE_MANIFEST.md`, `REQUIREMENT_MATRIX.md`, and the release receipt.
3. Resolve B-003. The helper deliberately refuses a nonzero `kanban.dispatch_stale_timeout_seconds` because supported `kanban dispatch` does not carry daemon stale-timeout parity. Either explicitly approve/set zero or deliver separately approved supported-CLI parity. Do not bypass the refusal.
4. Separately authorize and pass B-004, the real Darwin launchd coalition canary. A detached or session-leader child is not proof that `launchctl bootout` preserves descendants.
5. Separately authorize and perform B-005: discover every current board and query SQLite read-only for null/blank `notifier_profile`. Any unowned subscription blocks rollout pending routing proof or an independently approved migration.
6. Verify the retained absolute Hermes executable, Python 3.11 environment, source commit, `psutil`, and YAML support. Do not install, upgrade, or remove dependencies during cutover.
7. Prove all old gateway dispatcher-capable processes are stopped, not merely configured off. Reconcile canonical task/run rows with exact worker `(pid, creation time)` and ancestry. Terminal-card living workers still count. Unknown identity, PID reuse, stale rows, or ambiguous descendants block the operation.
8. Capture consistent SQLite backups plus exact config/plist receipts in a 0700 directory with 0600 files. Record whether each prior config key was unset so rollback can restore absence rather than invent a value.
9. Keep the current maintenance ESTOP byte-for-byte unchanged through drain. The helper neither owns nor clears it.

## Incompatible-config refusal

`check` and preflight must validate the effective **default-profile** contract before `run` is considered:

- `dispatch_in_gateway` is explicitly false, and no enabled old gateway still owns dispatch;
- `kanban.max_in_progress` is exactly 2;
- per-profile concurrency is exactly 1;
- orphan reconciliation is enabled;
- `dispatch_stale_timeout_seconds` is exactly 0 until supported parity exists;
- the configured Hermes source commit and CLI JSON contracts match the reviewed baseline;
- every configured board path is absolute, unique, schema-compatible, bounded, symlink-safe, and readable with SQLite `mode=ro` plus `query_only`;
- every subscription has a nonblank notification owner;
- state, lock, executable, source, and board paths have the expected ownership and restrictive modes.

A missing key, wrong type/value, source drift, CLI output drift, malformed/oversized output, missing board, unknown telemetry/process identity, or notification ambiguity is a refusal or hold—not a reason to patch files, retry a possibly successful command, or continue with reduced coverage.

## Singleton gateway coordination

The helper and gateway coordinate through the same `.dispatcher.lock` for their active lifetime. The helper lock is non-inheritable/CLOEXEC and children close file descriptors. Operational rules:

1. Disable embedded gateway dispatch through the supported default-profile config interface only after approval; no named-profile edits.
2. Drain and restart the old gateway safely so the boot-read setting actually takes effect.
3. Verify from process state and logs that no enabled old gateway remains; config text alone is insufficient.
4. Start only one helper. Lock contention is a hard refusal. Never delete or replace the lock file to defeat a live owner.
5. Observation-only `status` and `check` do not establish dispatch authority. A healthy check does not authorize `run`.
6. Manual CLI/API/agent starts bypass the helper and remain a documented race; prohibit them administratively during cutover and canary. The host cap is admission-only, not a global atomic limit.

## Future staged installation

Execute only with separate installation/config/service authorization:

1. Extract the verified archive to a versioned per-user operations directory outside installed Hermes source, for example `$HERMES_HOME/operations/kanban-resource-controller/<artifact-sha256>/`. Never copy it into the retained Hermes checkout or mutate installed Hermes source.
2. Create a distinct 0700 runtime-state directory. Create `controller.json` mode 0600 from `controller.example.json`, replacing every placeholder with reviewed absolute paths and the expected retained Hermes **source** commit (not the helper package commit).
3. Use the supported Hermes config interface for approved default-profile values. Record before/after receipts and prior unset state. Make no named-profile changes.
   The helper reads raw `config.yaml`, not CLI-resolved defaults, so every contract key must be written explicitly even where it equals the Hermes default: `kanban.max_in_progress 2`, `kanban.max_in_progress_per_profile 1`, `kanban.dispatch_stale_timeout_seconds 0`, `kanban.failure_limit`, `kanban.reconcile_orphans true`, `kanban.review_dispatch`, `kanban.auto_decompose`, `kanban.default_assignee`, `kanban.dispatch_profiles null`, and `kanban.dispatch_in_gateway false`. A missing key is a refusal by design.
4. Run observation-only `check`. It must report source/config/schema compatibility, honest live pressure, exact workers/descendants, all boards, and zero unowned subscriptions. Fixture recovery is not live recovery proof.
5. Render the reviewed plist with absolute paths but do not load it until B-004 passes, the old gateway lacks dispatcher capability, and the shared lock is available.
6. Load the LaunchAgent only in a separately authorized window. Keep the helper manually held. Confirm restrictive artifact/state modes, one service instance, the expected executable/import root, fresh status, bounded logs, and lock ownership.
7. Do not remove any old package. Package removal is a separate operation allowed only after retained-runtime, service, board-integrity, notification, and rollback proof.

## Admission policy and normal operation

The loop performs no LLM inference. Existing automatic decomposition may use its already configured auxiliary model when enabled.

Every 30 seconds, the helper samples load1, logical cores, available memory, native pressure, and cumulative swap-in/swap-out bytes via a per-OS telemetry backend (see "Cross-platform telemetry (schema 2)" below). It holds immediately when load1 is at least the core count, pressure is not normal, memory is below 4 GiB, the swap-out counter increases, or any sample is missing/malformed/non-finite/a telemetry read failure. Swap-in growth alone never holds or resets admission (D1 = A, recorded amendment; starved on this Mac under strict swap-in gating, since swap-ins were nonzero in 9 of 10 idle samples). First sample, counter reset/decrease, monotonic-time reversal, restart, or a long sample gap resets recovery.

Admission becomes eligible only after 120 uninterrupted seconds with load1 no greater than 0.8 times cores, at least 5 GiB available, normal pressure, and no swap-out increase. The exact 4 GiB boundary leaves the helper held until the 5 GiB recovery threshold is reached. Every attempted side-effecting command consumes the window and requires a new full recovery dwell.

Before and immediately before a command, reconcile exact process/run identities and enforce host cap 2, per-profile cap 1, and board concurrency `dispatch --max 1`. Existing excess workers are never killed and drain naturally. Under host pressure, ESTOP, manual hold, capacity, unowned-subscription, incompatible-config, or identity ambiguity, run no dispatch/decompose command.

In an eligible window, issue at most one side-effecting command against one board: one explicit-task decomposition or one dispatch, never both and never `--all`. For dispatch, bounded read-only dry runs predict each board, classify merge-conflict > review > build, apply round-robin ties, and age a passed board into one admission after six eligible windows. This is **best-effort downstream-first**, not a guarantee. If canonical post-command reconciliation differs from the prediction, record one `priority_miss`; never retry, kill, or issue another command in that window.

Timeout, launch failure, nonzero status, output/contract drift, or ambiguous post-command reconciliation writes persistent `uncertain-outcome`. Reconcile the pending journal, captured output, canonical rows, and processes manually. Do not delete/acknowledge evidence or restart admissions merely because the child deadline expired; bounded supervision sends no signal and waits for a finite child to end.

A `TelemetryError` (unreadable/malformed/oversized native counters, an unsupported platform, or — on Linux — PSI unavailable) writes reason `telemetry-error` with a machine-readable `error_code` and a sanitized, single-line, <=300-char `diagnostic` instead of the generic `persistent-operator-hold`. The policy's baseline and recovery dwell are invalidated (no stale counters are ever reused); recovery restarts from the next clean sample once telemetry is healthy again.

## Cross-platform telemetry (schema 2)

Historical bug: the pre-fix code called `psutil.swap_memory().sin/.sout` on Darwin. On macOS, `psutil` actually reports `vm_stat` **Pageins/Pageouts** (file-backed page traffic) through those fields, not real swap activity — so ordinary file I/O falsely looked like swapping and starved admission. Schema 2 fixes this at the root by giving every OS its own backend under `resource_controller/telemetry/`.

- **Darwin:** `/usr/bin/vm_stat` is parsed directly for the real `Swapins:`/`Swapouts:` lines and the page-size header; `psutil.swap_memory` is never called on Darwin. Native pressure still comes from `kern.memorystatus_vm_pressure_level` via `sysctl`.
- **Linux:** `/proc/vmstat` (`pswpin`/`pswpout`, multiplied by `os.sysconf("SC_PAGE_SIZE")`), `/proc/meminfo` (`MemAvailable`), and `/proc/pressure/memory` (PSI) are read directly, each capped at 64 KiB, no subprocesses. PSI mapping defaults (D2): `full avg10 > 0.0` is critical, otherwise `some avg10 >= 10.0` is warning, otherwise normal. Override the defaults only via an optional `controller.json` section:

  ```json
  {"telemetry": {"linux_psi": {"some_avg10_warning": 10.0, "full_avg10_critical": 0.0}}}
  ```

  An absent `telemetry` or `telemetry.linux_psi` section uses the defaults above — the Mac deployment's `controller.json` stays byte-identical. The section belongs in the controller's own `controller.json` runtime spec (never in Hermes `config.yaml`, whose kanban mapping still refuses unknown keys). The spec is read once at process start, so a threshold change takes effect only after a controller restart (hold, bootout, bootstrap). Values must be finite numbers in [0, 100]; unknown keys refuse like every other setting.
- **Linux without PSI** (D3, fail closed): `ENOENT`/`EOPNOTSUPP` reading `/proc/pressure/memory` raises `TelemetryError` with `error_code = "psi-unavailable"`, surfaced as `reason: telemetry-error`. Enable PSI (`psi=1` on the kernel command line, or the distro equivalent) rather than deploying with pressure disabled; there is no degraded fallback.
- **Container caveat:** Linux `/proc/pressure/memory` reflects the cgroup/container the controller runs in, not necessarily the physical host. This package is documented and intended to run directly on the host (launchd-only on macOS today); a future Linux deployment must run on bare metal or a privileged/host-PID container to get host-wide PSI.

### schema 1 -> schema 2 field mapping

| schema 1 field | schema 2 field | notes |
| --- | --- | --- |
| `sample.page_in` | `sample.swap_in_bytes` | Darwin: real `vm_stat` Swapins × page size (was `psutil` Pageins bug). Linux: `pswpin` × `SC_PAGE_SIZE`. |
| `sample.page_out` | `sample.swap_out_bytes` | Darwin: real `vm_stat` Swapouts × page size (was `psutil` Pageouts bug — the gating counter). Linux: `pswpout` × `SC_PAGE_SIZE`. |
| (absent) | `sample.counter_page_size_bytes` | Native page size used for the byte conversion above. |
| (absent) | `sample.units` | Always `"bytes"`; documents that the two fields above are already byte counts. |
| (absent) | `sample.source` | `"darwin-vm_stat+sysctl"` or `"linux-proc"`. |
| (absent) | `sample.pressure_detail` | Darwin: `{"level": <1|2|4>}`. Linux: PSI `some_avg10/some_avg60/full_avg10/full_avg60`. |
| (absent) | `swap_in_delta_bytes`, `swap_out_delta_bytes` | Non-negative deltas since the previous status write; `null` on the first sample or after a counter reset. Swap-in deltas are reported for diagnosis only and never gate admission (D1 = A). |
| (absent) | `sample_wall_time`, `interval_seconds`, `telemetry_age_seconds` | Wall-clock timestamp of the write, monotonic gap from the previous sample, and staleness, for diagnosing a stuck loop. |
| `pressure` (`normal`/`warning`/`critical`) | `pressure` | Unchanged enum values across the schema bump and across OSes. |

`status.json`/`check` gain a top-level `"schema_version": 2` key; schema 1 output carried no such key, so its absence is itself the schema-1 signal.

### Raw `vm_stat` / `/proc` comparison procedure (either OS)

To independently confirm the reported counters against the OS, without trusting the helper:

1. **Darwin:** run `vm_stat` yourself immediately before and immediately after a `check`/status sample, each with a timestamp. Read the raw `Swapins:`/`Swapouts:` cumulative counts (not `psutil`). The helper's reported `swap_in_bytes`/`swap_out_bytes` must fall between the before and after readings (before <= helper <= after) once both are expressed in bytes at the same page size — an exact-equality match is not expected because the two samples aren't simultaneous.
2. **Linux:** run `cat /proc/vmstat | grep -E 'pswpin|pswpout'` before and after, similarly bracketing the helper's `swap_in_bytes`/`swap_out_bytes` (divided by `getconf PAGE_SIZE` to compare page counts, or multiply the raw counters by the same page size to compare bytes).
3. A large `Pageins`/file-cache delta with flat `Swapouts` (e.g. `cat` a few GiB of files to `/dev/null`) must **not** move the helper's `swap_out_bytes` or trigger a `swap-out` hold — this is the specific regression this fix targets.

### Linux prerequisites (future deployment; not part of this Mac rollout)

- Kernel PSI enabled (`CONFIG_PSI=y` and `psi=1` if gated at boot) so `/proc/pressure/memory` exists — otherwise the backend fails closed with `psi-unavailable` (D3).
- The controller must run directly on the host, not inside a container, so `/proc/pressure/memory` reflects real host memory pressure rather than a cgroup's view (see the container caveat above).
- This package and its launchd plist are macOS/launchd-only today. Nothing here is a Linux deployment or activation plan — the Linux backend and Linux CI gate exist so the code is portable and covered, not because Linux rollout is authorized. Any future Linux service supervisor, packaging, or install path is separate, unauthorized work.



## Hold, recovery, and safe shutdown

- `hold` creates only the helper's admission hold. It does not stop existing workers or alter Hermes ESTOP.
- `resume` removes only `manual-hold.json`, records the reason, and restarts the full recovery dwell. It does not imply healthy telemetry or capacity.
- A persistent uncertainty, incompatible contract, stale run, unknown descendant, or unowned subscription requires operator disposition and normally a new reviewed snapshot or separately approved operational fix.
- For shutdown, engage helper hold first and wait until no command, worker, known descendant, or unknown coalition member remains. Recheck canonical rows and exact process identities immediately before service action.
- Do not `bootout`, unload, `kickstart -k`, replace, update, or overwrite the service while descendants exist. Never signal, relabel, reclaim, or repair a worker merely to make drain appear complete.
- If drain or identity proof cannot complete, leave the helper held and service loaded, record the exact blocker, and stop. Urgency does not authorize descendant termination.

## Separately authorized bounded canary

### Phase A — isolated synthetic canary

Use a temporary isolated HOME/HERMES_HOME, synthetic board database, harmless finite worker, stub telemetry, and the real supported CLI path. Do not point any path at production state, dispatch production work, or stress the host. Verify:

1. incompatible config and second lock owner refuse safely;
2. pressure, ESTOP, manual hold, each capacity cap, unowned subscription, and identity ambiguity produce zero starts;
3. first sample plus the complete 120-second recovery interval is required;
4. one eligibility window produces at most one side-effecting command;
5. board `--max 1`, exact task/worker identity, decomposition-by-explicit-id, priority selection, aging, and `priority_miss` behavior match the requirement matrix;
6. timeout sends no signal, tracks/drains the finite child, and persists uncertainty;
7. shutdown refuses while any command, worker, known descendant, or unknown coalition member remains.

Abort Phase A on any unexpected process, path escape, write outside the temporary roots, second side effect, signal, missing/oversized output, contract drift, leaked lock/fd, stale status, or failed assertion. Preserve all evidence and do not progress.

### Phase B — Darwin coalition canary

This phase needs its own explicit authorization. Use only a harmless finite child under a disposable LaunchAgent. Observe real launchd ancestry/coalition behavior and prove the runbook's hold→natural drain→offline ordering. Abort before any bootout/restart if identity is unknown or a non-canary descendant appears. Never extrapolate from detached/session-leader behavior.

### Phase C — production zero-start observation

This phase requires separate production/canary authorization after A and B pass. Keep helper manual hold and the exact maintenance ESTOP unchanged. Verify executable/import roots, singleton lock, fresh telemetry/status, board integrity, notifier ownership, and disabled embedded dispatch with **zero eligible work admitted**. Abort on config/source drift, any unowned subscription, unknown worker/descendant, lock contention, stale status, unexpected board mutation, any start/decomposition, or inability to prove zero-start behavior.

Activation is not part of the canary. Clearing the exact unchanged maintenance ESTOP, removing manual hold, or allowing production admission is a later explicit release-owner decision with its own abort plan.

## Rollback

Rollback is admission-safe, not availability-first:

1. Engage helper hold and preserve status, logs, receipts, and pending journals.
2. Drain commands, workers, descendants, and coalition members naturally; unknown state blocks unload.
3. Leave embedded gateway dispatch disabled while failure or ambiguity remains. An idle queue is safer than dual dispatch.
4. After a proven safe service exit, restore only the exact previously reviewed plist/artifact or leave the helper offline.
5. If returning dispatch to the gateway, restore every exact prior default-profile value—including removing keys previously unset—through supported CLI. Restart only after all affected work drains and exclusive lock ownership is proven.
6. Re-verify board integrity, service exclusivity, process ancestry, notification routing, and exact config receipts before considering rollback complete.

Rollback never clears Hermes ESTOP, deletes uncertain evidence, removes board state/worktrees, force-terminates workers, activates the prior load-adaptive branch, changes named profiles, or uninstalls the retained package.

## Legacy unowned subscriptions and delivery limitation

Gateway notification collection includes unowned subscriptions only when the gateway owns the dispatcher lock. External helper ownership can therefore stop legacy unowned delivery. Prior read-only queries found zero unowned subscriptions across four discovered board databases. That is historical point-in-time evidence only; it neither proves current state nor guarantees future delivery.

Every authorized rollout/canary must rediscover and reread every current board. A null/blank owner is a hard activation abort. This helper never migrates, fabricates, or rewrites ownership, and fixture/readiness success does not prove end-to-end notification delivery.

## Unresolved blockers at packaging time

- B-003: approve production stale-timeout zero/parity; current nonzero policy remains incompatible.
- B-004: separately authorize and pass real Darwin launchd coalition behavior.
- B-005: separately authorize live read-only board discovery, zero-unowned recheck, and routing proof.
- Installation, default-profile config changes, service changes, production zero-start canary, activation, ESTOP transition, live dispatch/decomposition, and package removal each remain unauthorized.
- Canonical-inventory assignee cross-check hardening is an out-of-scope follow-up; GitHub issue publication remains pending explicit shared-repository approval.
- Smithers #2041 remains separate and backlog-only.

No production activation or production canary occurred while building, verifying, or packaging this artifact.
