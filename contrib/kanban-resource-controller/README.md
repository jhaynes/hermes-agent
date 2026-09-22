# External Kanban resource admission controller (Darwin)

This is a **local operational artifact**, not a Hermes plugin, fork installation,
core scheduler modification, or an installed service. It requires the retained
Python 3.11 environment with its existing `psutil`. It performs no model inference
in its monitoring/control loop. An explicitly enabled `kanban decompose` action
still uses Hermes's existing auxiliary model.

The binding task is `t_206ec88f`; independent review and activation belong to
`default/t_da3d77bf`. Read `IMPLEMENTATION.md` before deploying. Do not activate
merely because the tests pass.

## Contract and prerequisites

The source contract is pinned to
`5910de20bc9839fdd36e791a9d72ba2c2e722f66`. Before every inventory and action, the
helper rejects tracked source differences from that baseline outside this artifact
folder. This intentionally refuses upgrades until their contract is independently
reviewed. The configured source tree must be a Git checkout containing that base.
The helper imports the real top-level argv parser and read-only config loader, but
never imports private dispatcher machinery.

Active mode requires:

- Default-profile effective `kanban.dispatch_in_gateway: false` and exclusive
  ownership of its existing `kanban/.dispatcher.lock`. **Changing config alone
  does not stop an already-running embedded dispatcher.** The release owner must
  arrange safe gateway handoff without interrupting its active work.
- Effective `max_in_progress: 2`, `max_in_progress_per_profile: 1`,
  `reconcile_orphans: true`, and `dispatch_stale_timeout_seconds: 0`.
- A boolean `auto_decompose` and a positive integer `failure_limit`. The latter is
  forwarded explicitly to supported CLI dispatch. The former is reread each tick.
- Compatible canonical tables/columns, coherent task/run/process identity, and no
  legacy notification subscriptions with absent/blank `notifier_profile`.

**Activation blocker, not an instruction to change policy:** the retained default
`dispatch_stale_timeout_seconds` is 14400, but the supported `kanban dispatch` CLI
passes its implementation default 0 and exposes no stale-timeout flag. The helper
holds rather than silently dropping that policy. The release owner must obtain a
specific disposition (approved config amendment, or separately approved upstream
capability) before activation if the effective setting is nonzero. This builder
has not changed the setting or the live interim max5/embedded-dispatch policy.

Other reviewed intentional differences: 30-second resource sampling rather than
the gateway's polling interval; one action per eligibility window rather than a
multi-task decomposition sweep; conservative **board concurrency 1**, not a
one-start burst flag. Ready/todo/review work on the selected board wins over
triage, so triage cannot starve ready builds. All live board slugs rotate, including
empty/ineligible boards. No `--all`, `--max 0`, reclaim, repair, pause or resume
Hermes command is ever issued by the controller.

## Resource and capacity policy

- Admission holds at load1 >= logical cores, native pressure != normal (1),
  available memory < 4 GiB, or an increase in either paging direction.
- Recovery requires 120 consecutive seconds of load1 <= 0.8 * cores, available
  memory >= 5 GiB, pressure normal, and unchanged paging counters.
- Cold start, unknown/malformed sensors, counter resets, nonmonotonic timestamps
  or a sample gap > 60 seconds reset dwell. The first quiet comparison starts
  dwell, so nominal cold-start admission is conservatively about 150 seconds.
- Every possible side-effecting command consumes the window, even if it starts
  nothing or fails. Before a command, canonical processes/runs/config/ESTOP are
  reread and native resource sensors are sampled again.
- At most two matching workers for this OS user across all boards; at most one
  per profile; at most one per board. Existing excess workers are never killed.
  Terminal cards still count until their actual PID/creation-time identity exits.
  Canonical `spawned` events preserve fingerprints after Hermes clears task/run
  PID columns on completion. Observed worker descendants are retained across
  reparenting and restart.
- Stale claims, missing fingerprints, conflicting identities, unrecognized exact
  worker query argv, and inaccessible same-effective-user processes hold visibly.
  No task status/PID/notification ownership is fabricated or repaired.

This is admission control, **not an atomic host-wide semaphore**. Manual CLI/API
starts, other users, cron/messages/Desktop, and worker-internal fan-out bypass it.
Manual starts can race the final recheck. A single admitted build can overload the
host. Paging-in includes the native psutil counter's semantics; sustained paging
may prevent recovery indefinitely. Historical swap occupancy alone is not used.

## Configuration and local commands

Copy `controller.example.json` and replace every placeholder with an absolute path:

- `source`: retained Hermes source checkout (not this helper's installation path).
- `python`: retained venv Python executable.
- `home`: default/fleet Hermes root, also the canonical Kanban root.
- `user_home`: OS user's home (explicit, separate from the Hermes root).
- `state`: dedicated per-user controller state directory.

Config must be a regular file owned by the current user, mode 0600. State must be
an owned non-symlink directory, mode 0700. State files are restrictive atomic
write/fsync/replace operations. No secrets belong in this JSON. The CLI subprocess
environment is allowlisted, with explicit roots, interpreter PATH, native threads
1, and no inherited Kanban task/board/profile/delegation markers.

Using explicit absolute values for `PYTHON`, `APP`, and `CONFIG`:

```sh
"$PYTHON" -B "$APP" sample
"$PYTHON" -B "$APP" --config "$CONFIG" observe
"$PYTHON" -B "$APP" --config "$CONFIG" run
"$PYTHON" -B "$APP" --config "$CONFIG" hold
"$PYTHON" -B "$APP" --config "$CONFIG" resume
"$PYTHON" -B "$APP" --config "$CONFIG" stop
```

`sample` only reads host sensors. `observe` is one read-only decision and never
claims the dispatcher lock or calls dispatch/decompose (it owns only a private
state-directory lock to avoid overwriting an active controller's status). Use a
separate state path for side-by-side observation. Supported `boards list --json`
is run against private **metadata-only replicas** because that CLI opens/migrates
DBs merely to count tasks. Live databases are opened `mode=ro`/`query_only` inside
consistent read transactions with schema validation, query timeout and row bounds.
There is no copying of task bodies or secrets into the replicas.

`hold` stops new admissions only. `resume` here removes the **controller's own**
hold, never Hermes ESTOP; it refuses pending uncertainty and resets resource dwell.
`stop` asks the running service to hold and exit **only after proven drain**; it
never sends a signal to workers. Watch `status.json` for `manual:draining`, then
`manual:drained`, and verify the service PID actually exits.

Status reasons include `resource:*`, `capacity:*`, `identity:*`, `compatibility:*`,
`ESTOP`, `manual:*`, `observation:*`, `cooldown:*`, and `uncertain-outcome`.
`events.log` is bounded to 64 KiB plus two backups. CLI output is bounded to 64 KiB;
read commands have a 15-second deadline, dispatch 30 seconds, decomposition 120
seconds. These are observation deadlines, **not kill timers**. A timed-out command
is still drained/reaped by its controller; it and any workers are never killed or
silently retried.

## Uncertain outcome recovery

`pending.json` is fsynced **before** starting a side-effecting CLI, then stamped
with its PID/creation time. A crash in either launch window, timeout, output bound,
nonzero exit, malformed JSON or post-action disagreement persists an operator
hold across restart. `reconciliation.json` records pre/post canonical snapshots;
no retries occur. `reader/pending.json` similarly fences failed read commands.

1. Request controller `hold`. Inspect both pending journals, canonical board/run
   state and exact process identities. Let living commands/workers finish. Do not
   repair DB rows simply to make the check green.
2. Request `stop`; wait for proven natural service exit. If identity is unknown,
   stop refuses to exit. Do not force-unload the service to bypass this.
3. Once offline, with the gateway dispatcher still disabled, use:

   ```sh
   "$PYTHON" -B "$APP" --config "$CONFIG" \
     --reason "documented operator reconciliation evidence" acknowledge
   ```

   This requires exclusive private and shared locks, manual hold, dead known
   command identities, no observed descendants, and fresh compatible canonical
   inventory without workers/holds. It archives the latest acknowledged journals
   and leaves the manual hold intact. A PID-less uncertain launch cannot be
   automatically acknowledged: explicit external investigation remains necessary.
4. Restart the reviewed service, then explicitly `resume`; fresh dwell applies.

Never delete journals/state or unlink lock files as routine recovery. Unlinking a
held lock splits its inode authority.

## Installation template (release owner only, after review)

No installer is run by the builder. Target deployment is the explicit operational
directory selected by the release owner, e.g. the user's
`operations/kanban-resource-controller/` below their Hermes root, **outside installed
Hermes source**. Copy only `controller.py`, `resource_controller/`, the reviewed
README/IMPLEMENTATION, and a private configured JSON. Retain the prior reviewed
artifact and a SHA/hash manifest. No pip install, plugin registration or source edit.

1. Check current processes/claims, effective config, notifications, ESTOP, and all
   gateway consumers. Preserve the latest authorized config changes and unpaused
   cron/messages. Do not replay obsolete migration or broad-pause instructions.
2. Make restrictive backups of affected config/plist plus SQLite backups using
   SQLite's backup API (never plain-copy an active WAL DB). The helper never edits
   config; use supported `hermes config set` only for approved amendments.
3. Have the release owner perform safe embedded-dispatch handoff. Never restart
   a gateway with active affected workers/nonworker activity. Existing excess
   workers finish; no forced drain to reach cap2.
4. Run observation and resolve all compatibility/identity holds. Review the
   exact frozen artifact SHA, tests and source contract again.
5. Fill the absolute placeholders in the supplied `.plist.in`; validate with
   `plutil -lint`. Save the resulting plist mode0600 under the chosen user's
   `Library/LaunchAgents/ai.hermes.kanban-resource-controller.plist`. ProgramArguments
   are an argv array, not a shell command. Use `launchctl bootstrap gui/UID PLIST`
   only after the independent release gates pass.
6. Read back `launchctl print gui/UID/ai.hermes.kanban-resource-controller`, actual
   process executable/argv, shared lock exclusion, and fresh atomic status.
   Confirm no automatic starts during the real resource/capacity/ESTOP hold,
   and that cron/messages/Desktop remain available. Do not infer live healthy
   recovery from deterministic fixtures.

The template applies niceness10/native threads1, private umask, and restart-on-error
but **not** restart-on-success. Thus a successful `stop` stays stopped. Application
logs are bounded; launchd stdout/stderr go to `/dev/null`. Investigate startup
failures by running the same argv in the foreground with admissions held.

## Safe lifecycle, updates, rollback and limits

**Never use `launchctl bootout`, `kickstart -k`, service restart, or forced signals
while workers/commands/descendants might belong to its coalition.** `setsid` and
parent PID1 do not prove separation from a Darwin launchd coalition. SIGTERM/SIGINT
request a drain, but launchd may escalate to SIGKILL anyway; handlers are not a
bootout safety guarantee.

Request admission-only hold, then natural `stop`. Check `manual:drained`, the actual
service exit, canonical runs, captured descendant identities, and the release
owner's launchd/coalition inspection before administrative unload. Only then may
the owner run `launchctl bootout gui/UID/ai.hermes.kanban-resource-controller`, swap
reviewed artifacts, revalidate config and bootstrap. Never delete a dirty/unique
implementation worktree as cleanup.

The builder's native test demonstrates that a live detached harmless child blocks
natural service exit and survives until its release marker, then permits exit.
It does **not** execute launchctl bootout or claim arbitrary detached children
survive unload. No launchd service changes are authorized in the builder phase.
Independent release must validate its actual supervisor topology with harmless
children before activation. The tracker cannot certify children that daemonize
and disappear from ancestry between observations; such unknown coalition membership
requires operator inspection, not an unconditional safe-unload claim.

Rollback: keep admissions held, finish the same safe drain, retain state/journals,
unload only after proof, restore the previous reviewed helper/plist and verify it.
If returning dispatch to the gateway, the release owner must restore prior values
through supported config commands, including prior **unset** values rather than
assuming a numeric default, and safely restart only after affected work is drained.
Do not enable embedded dispatch while the helper owns authority. If anything is
uncertain, leave embedded dispatch false and retain the controller hold: an idle
queue is safer than uncontrolled starts. The controller never clears ESTOP.

## Validation

Run from the linked worktree with its retained interpreter:

```sh
nice -n 10 /absolute/retained/venv/bin/python \
  contrib/kanban-resource-controller/run_tests.py \
  --receipt-dir /absolute/unique/scratch/receipts --label final
```

The runner uses serial stdlib unittest with temporary HOME/HERMES_HOME/KANBAN_HOME,
credential-free subprocess environment, native threads1 and durable JSON receipts.
It deliberately does not use the repository pytest runner: the retained
`scripts/run_tests.sh:166` unconditionally invokes `compileall -j 0`, expressly
forbidden by this task. No existing root test fixtures or manifests were changed.
Tests use real source-pinned imports, supported CLI, real SQLite and harmless
processes. The real CLI canary is not a production queue test or an LLM call.
