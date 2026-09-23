# Operational runbook — NOT AUTHORIZED FOR THIS TASK

All steps below are a future release-owner procedure. This implementation task performed none of them.

## Preconditions

1. Review the exact frozen commit, cumulative diff from `5910de20bc9839fdd36e791a9d72ba2c2e722f66`, requirement matrix, and serial receipts. Dedicated `reviewscope` approval is mandatory.
2. Resolve B-003 by explicitly setting/approving `kanban.dispatch_stale_timeout_seconds: 0`, or add separately approved supported CLI parity. The helper refuses the current nonzero default.
3. Perform the separately authorized Darwin launchd coalition canary (B-004). A detached/session-leader process is not proof that `launchctl bootout` preserves descendants.
4. Recheck all discovered boards in SQLite read-only mode for null/blank `notifier_profile` (B-005). Any such row blocks rollout; do not rewrite ownership in this helper.
5. Prove all old gateway dispatcher processes are stopped, not merely configured off. Prove canonical worker/run/process ancestry and drain before service changes.
6. Take consistent SQLite backups and config/plist receipts with 0600 files in a 0700 directory. Preserve prior unset values exactly.

## Staged installation (separately authorized)

1. Copy the frozen artifact to a versioned per-user operations directory outside installed Hermes source, e.g. `$HERMES_HOME/operations/kanban-resource-controller/<sha>/`.
2. Create runtime state in a separate 0700 operations state directory. Create `controller.json` as 0600 using `controller.example.json`; replace every placeholder with an absolute path and the reviewed source SHA.
3. Confirm the retained Python environment imports `psutil` and `yaml`. Do not install or remove packages during cutover.
4. Set the approved default-profile values through supported `hermes config set`; make no named-profile changes. Keep the existing maintenance ESTOP unchanged during drain.
5. Use `check` first. It must report current live pressure honestly, source/config/schema compatibility, exact workers, and zero unowned subscriptions. Fixture recovery is not live recovery proof.
6. Install the reviewed plist only after B-004. Replace placeholders with absolute paths. Do not load it while the old gateway owns `.dispatcher.lock` or has dispatcher capability live.

## Canary

Use an isolated synthetic board/home and harmless finite worker through the real supported CLI. Verify: singleton exclusion; no start under pressure/ESTOP/manual/cap holds; full 120-second recovery; one side-effect command; `--max 1` board concurrency; post-command exact identity; timeout drains without signals; and priority-miss logging. Do not point the canary at production queues or stress the host.

For the final live zero-start check, keep the helper manually held, acquire authority, and verify status/lock/telemetry with no eligible work admitted. Activation and clearing the exact unchanged maintenance ESTOP require a separate explicit release-owner step.

## Lifecycle

- `hold` prevents new admission only.
- `resume` removes only `manual-hold.json` state and restarts recovery dwell; it never changes `$HERMES_HOME/ESTOP`.
- A service stop request first engages the helper hold. Do not `bootout`, `kickstart -k`, unload, update, or replace the service while a command, worker, known descendant, or unknown coalition member remains.
- Wait for canonical runs and exact `(pid, creation time)` identities to drain naturally. Never kill or relabel a worker to make a drain pass.
- Persistent `uncertain-outcome` requires operator reconciliation of the pending journal, CLI output, canonical runs, and processes. Do not delete or auto-acknowledge it.

## Rollback

1. Engage helper hold and preserve status/journals.
2. Drain commands, workers, descendants, and coalition members naturally.
3. Leave embedded gateway dispatch disabled while helper failure or ambiguity remains; an idle queue is safer than dual dispatch.
4. After safe service exit, restore only the previously reviewed artifact/plist.
5. If returning dispatch to the gateway, restore every exact prior config value (including unset values) through supported CLI and restart only after affected work drains.
6. Never clear Hermes ESTOP, delete uncertain evidence, remove worktrees, force worker termination, or uninstall the retained package as rollback.

## Legacy notifications

The gateway includes unowned subscriptions only when it owns the dispatcher lock. External lock ownership can therefore stop legacy unowned delivery. Historical read-only checks found zero such rows in four databases, but every rollout preflight rereads every current board. This helper never migrates, fabricates, or rewrites notification ownership.
