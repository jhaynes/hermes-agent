# Requirement-to-test matrix

All automated tests run serially. `PASS` below means implemented unit/contract coverage in this snapshot; it is not activation approval.

| ID | Status | Evidence |
|---|---|---|
| T-001 thresholds | PASS | `test_threshold_boundaries_fail_closed` covers load at `cores`, memory immediately below 4 GiB, the exact 4 GiB transition band, exact 5 GiB recovery boundary, and warning/critical pressure |
| T-002 paging | PASS | `test_threshold_boundaries_fail_closed`, `test_unknown_reset_and_nonmonotonic_samples_fail_closed` cover both paging directions and counter resets |
| T-003 recovery dwell/gaps/cooldown | PASS | `test_first_sample_holds_then_full_quiet_dwell_recovers`, `test_recovery_requires_strict_recovery_band_and_command_resets_it`, `test_completed_command_consumes_the_full_recovery_window` cover exact 120 seconds, gaps, restart/counter reset, and post-command cooldown |
| T-004 unknown telemetry | PASS | policy and `test_host_lifecycle.HostSamplerTests` |
| T-005 singleton/CLOEXEC | PASS | `test_lock_storage_supervision.SingletonLockTests` |
| T-006 incompatible config/source | PASS | `test_config_cli.ConfigContractTests`, `test_live_cli_contract`, `test_preflight.PreflightTests`, `test_spec.RuntimeSpecTests` |
| T-007 one-task decomposition | PASS | CLI contract/adapter and engine decomposition tests plus `test_sandbox_cli_e2e` prove an explicit task, never `--all` |
| T-008 dispatch/caps/--max1 | PASS (isolated) | `test_capacity_is_host_profile_and_board_admission_only`, engine limit holds, and sandboxed executable argv receipt prove host 2/profile 1/board `--max 1` without production dispatch |
| T-009 one command/window | PASS | decomposition/dispatch/uncertainty tests and `test_completed_command_consumes_the_full_recovery_window` |
| T-010 downstream-first/aging | PASS | classification/selection/aging/priority-miss tests plus `test_aging_never_bypasses_estop_manual_hold_or_host_cap` |
| T-011 pre-command races | PASS (isolated) | fingerprint race and final-fence capacity race both abort before side effect and do not count an aborted aging window |
| T-012 post-command ambiguity | PASS (isolated) | divergent actual task logs `priority-miss`; timeout, launch/output failure, parse drift, or reconciliation ambiguity persist `uncertain-outcome` and do not retry |
| T-013 exact process identity | PASS | `test_inventory.WorkerArgvTests` and `ReconciliationTests` |
| T-014 timeout supervision/no signal | PASS | `test_timeout_observes_without_killing_and_waits_for_finite_child` |
| T-015 ESTOP preservation | PASS | `test_estop_is_read_only_and_prevents_every_command`, manual-hold test |
| T-016 board enumeration | PASS for configured inventory | explicit complete board map, read-only schema/row/symlink/hash tests, and sandbox CLI command receipt; Hermes has no accepted `boards list --json` dependency in this artifact |
| T-017 read-only inventory | PASS | `test_board_inventory` schema, row bound, symlink, hash invariants |
| T-018 unowned subscriptions | PASS (isolated) | inventory count plus `test_unowned_subscription_and_each_capacity_limit_hold_before_dispatch`; live recheck remains rollout gate B-005 and historical zero counts are not projected forward |
| T-019 restrictive storage/journal | PASS | `test_lock_storage_supervision.SecureStorageTests`, runtime-spec mode tests |
| T-020 lifecycle descendants | PASS (state boundary) | `test_host_lifecycle.LifecycleTests`; real launchd topology remains B-004 |
| T-021 rollback | DOC/PASS boundary | `LifecycleGuard`, pending journal, and `RUNBOOK.md`; operational rollback is separately gated |
| T-022 host sampler evidence | PASS (isolated), LIVE BLOCKED | injected Darwin sensor mapping and unknown/error paths pass; no live health or recovery claim is made and live read-only receipt remains separately gated |
| T-023 no production mutation | PASS for build/verification | only isolated worktree files and task records changed; fake executable, synthetic SQLite, injected telemetry/process/clock, and temporary state only |
| T-024 source/static boundaries | PASS | standalone artifact imports no Hermes private module; serial compile, plist lint, diff check, and added-line security scan are handoff gates |

## Failure-path evidence

| Failure path | Expected safe result | Deterministic evidence |
|---|---|---|
| Missing/malformed sensor, pressure identity, paging reset, time reversal/gap | telemetry/recovery hold; dwell reset | `test_unknown_reset_and_nonmonotonic_samples_fail_closed`, `HostSamplerTests` |
| Incompatible config/source/CLI output | refuse or board-ineligible; no side effect | `ConfigContractTests`, `PreflightTests`, `CliContractTests` |
| Lock contention | second owner refused; recovery only after release | `test_one_owner_noninheritable_and_recovery_after_release` |
| Unknown/PID-reused/inaccessible/stale process identity | reconciliation hold; no row repair | `ReconciliationTests` |
| Gate changes between selection and command | `precommand-race`/capacity hold; no command | fingerprint and final-fence race tests |
| Timeout, nonzero, oversized/malformed output, uncertain reconciliation | persistent `uncertain-outcome`; wait without signal; no retry | `SupervisionTests`, CLI adapter and restart tests |
| ESTOP/manual hold/unowned subscription/cap | no prediction-side effect or dispatch/decompose | engine common-hold, aging, and capacity-limit tests |
| Actual task differs from prediction | one command only, `priority_miss` increment, no retry/kill | `test_highest_stage_dispatches_once_and_logs_priority_miss` |
| Command just consumed a window | no second command before a new full recovery interval | `test_completed_command_consumes_the_full_recovery_window` |
| Stop with command/worker/unknown descendant | lifecycle refusal; no bootout promise | `test_stop_refuses_commands_workers_and_unknown_descendants` |

## Reproducible verification command

From `contrib/kanban-resource-controller` in the isolated worktree:

    /Users/jhaynes/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
    /Users/jhaynes/.hermes/hermes-agent/venv/bin/python -m compileall -q -j 1 resource_controller tests
    /usr/bin/plutil -lint launchd/ai.hermes.kanban-resource-controller.plist

The external package receipt records the exact frozen SHA/tree, archive SHA-256, per-file manifest, final count/output, and cumulative `git diff --check` receipt. Tests are serial at the file runner level; only finite child execution inside the timeout case provides controlled concurrency.

## Open activation/release requirements

- B-003: production stale-timeout policy is expected to be incompatible until explicitly approved/changed or supported CLI parity exists.
- B-004: real Darwin launchd coalition canary is not authorized and not performed.
- B-005: live read-only unowned-subscription recheck is not performed by this build task.
- B-006: the implementation snapshot `4d081104d8039869ae64c74a202575ca2e2924e8` received exact-SHA quality and mandatory cross-company `reviewscope` approval. The final packaging snapshot must receive the same exact-SHA review before release handoff.
- Real launchd topology, live read-only telemetry, production board metadata, and notification routing remain release gates; fixture or sandbox success is not represented as live proof.
- No installation, config/service mutation, activation, production dispatch/decomposition, package removal, ESTOP change, or worker signal occurred.
