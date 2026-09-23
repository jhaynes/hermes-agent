# Requirement-to-test matrix

All automated tests run serially. `PASS` below means implemented unit/contract coverage in this snapshot; it is not activation approval.

| ID | Status | Evidence |
|---|---|---|
| T-001 thresholds | PASS | `test_policy_priority.AdmissionPolicyTests.test_threshold_boundaries_fail_closed` |
| T-002 paging | PASS | `test_threshold_boundaries_fail_closed`, `test_unknown_reset_and_nonmonotonic_samples_fail_closed` |
| T-003 recovery dwell/gaps/cooldown | PASS | `test_first_sample_holds_then_full_quiet_dwell_recovers`, `test_recovery_requires_strict_recovery_band_and_command_resets_it` |
| T-004 unknown telemetry | PASS | policy and `test_host_lifecycle.HostSamplerTests` |
| T-005 singleton/CLOEXEC | PASS | `test_lock_storage_supervision.SingletonLockTests` |
| T-006 incompatible config/source | PASS | `test_config_cli.ConfigContractTests`, `test_preflight.PreflightTests`, `test_spec.RuntimeSpecTests` |
| T-007 one-task decomposition | PASS | CLI contract/adapter and engine decomposition tests |
| T-008 dispatch/caps/--max1 | PASS (contract/unit) | CLI command tests and `test_inventory.test_capacity_is_host_profile_and_board_admission_only`; isolated real-CLI canary deferred to verification |
| T-009 one command/window | PASS | engine decomposition, dispatch, uncertainty tests |
| T-010 downstream-first/aging | PASS | priority classification/selection/aging and engine priority-miss tests |
| T-011 pre-command races | PASS (representative) | `test_engine.test_precommand_fingerprint_change_prevents_side_effect`; expanded race matrix deferred to verification |
| T-012 post-command ambiguity | PASS (representative) | persistent uncertainty and priority-miss tests; expanded fingerprint cases deferred to verification |
| T-013 exact process identity | PASS | `test_inventory.WorkerArgvTests` and `ReconciliationTests` |
| T-014 timeout supervision/no signal | PASS | `test_timeout_observes_without_killing_and_waits_for_finite_child` |
| T-015 ESTOP preservation | PASS | `test_estop_is_read_only_and_prevents_every_command`, manual-hold test |
| T-016 board enumeration | PARTIAL | explicit board map + read-only schema tests; supported `boards list --json` metadata-replica canary deferred to verification |
| T-017 read-only inventory | PASS | `test_board_inventory` schema, row bound, symlink, hash invariants |
| T-018 unowned subscriptions | PASS | inventory count + engine hold; live recheck remains rollout gate B-005 |
| T-019 restrictive storage/journal | PASS | `test_lock_storage_supervision.SecureStorageTests`, runtime-spec mode tests |
| T-020 lifecycle descendants | PASS (state boundary) | `test_host_lifecycle.LifecycleTests`; real launchd topology remains B-004 |
| T-021 rollback | DOC/PASS boundary | `LifecycleGuard`, pending journal, and `RUNBOOK.md`; operational rollback is separately gated |
| T-022 real host evidence | BLOCKED FOR VERIFICATION | read-only `check` exists; no live claim is made in implementation |
| T-023 no production mutation | PASS for implementation | only isolated worktree files/task records changed; verification must capture external before/after receipts |
| T-024 source/static boundaries | PASS pending static gate | artifact imports no Hermes private module; final static scans/gates required before handoff |

## Open activation/release requirements

- B-003: production stale-timeout policy is expected to be incompatible until explicitly approved/changed or supported CLI parity exists.
- B-004: real Darwin launchd coalition canary is not authorized and not performed.
- B-005: live read-only unowned-subscription recheck is not performed by this build task.
- B-006: exact frozen-snapshot review, including mandatory `reviewscope`, remains required.
- Isolated real supported-CLI board canary, live read-only telemetry receipt, and full race/failure matrix belong to child verification task `t_36e6c534`.
- No installation, config/service mutation, activation, production dispatch/decomposition, package removal, ESTOP change, or worker signal occurred.
