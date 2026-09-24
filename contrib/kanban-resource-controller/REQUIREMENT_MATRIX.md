# Requirement-to-test matrix

All automated tests run serially. `PASS` below means implemented unit/contract coverage in this snapshot; it is not activation approval.

| ID | Status | Evidence |
|---|---|---|
| T-001 thresholds | PASS | `test_threshold_boundaries_fail_closed` covers load at `cores`, memory immediately below 4 GiB, the exact 4 GiB transition band, exact 5 GiB recovery boundary, and warning/critical pressure |
| T-002 paging | PASS (amended, see REQUIREMENT_LEDGER §17) | `test_swap_out_growth_holds_but_swap_in_growth_alone_never_does`, `test_unknown_reset_and_nonmonotonic_samples_fail_closed` cover D1=A swap-out-only gating and counter resets; `tests/test_telemetry.py` (Darwin/Linux parsers, backend selection, PSI thresholds, `psutil.swap_memory` never called on Darwin, telemetry-error path, end-to-end divergent-source regression) covers the cross-platform fix root cause, `test_engine.StatusDeltaTests`, `test_main.RunLoopTelemetryTests`, `test_spec.TelemetrySpecTests` |
| T-003 recovery dwell/gaps/cooldown | PASS | `test_first_sample_holds_then_full_quiet_dwell_recovers`, `test_recovery_requires_strict_recovery_band_and_command_resets_it`, `test_completed_command_consumes_the_full_recovery_window` cover exact 120 seconds, gaps, restart/counter reset, and post-command cooldown |
| T-004 unknown telemetry | PASS | policy and `test_host_lifecycle.HostSamplerTests` |
| T-005 singleton/CLOEXEC | PASS | `test_lock_storage_supervision.SingletonLockTests` |
| T-006 incompatible config/source | PASS | `test_spec.RuntimeSpecTests` covers strict admission parsing and sole absent-section defaults; `test_config_cli.ConfigContractTests` covers exact Hermes host/max-profile parity and explicit profile registry; `test_main.CheckOutputTests` makes expected/observed mismatch visible; existing source/live-CLI/preflight refusal remains |
| T-007 one-task decomposition | PASS | CLI contract/adapter and engine decomposition tests plus `test_sandbox_cli_e2e` prove an explicit task, never `--all` |
| T-008 configurable caps/C′ `m` | PASS (isolated) | non-default host 4, builder 2, board override 4 boundaries; selected-board `hermes_db_running_count`; terminal-card divergence; `m` 1/2/3/4 and cap hold; sandbox executable ramps explicit `--max 1` then `--max 2` without production dispatch |
| T-009 one mutating command/window | PASS | decomposition/dispatch/uncertainty tests, `test_completion_race_reconciles_all_identities_and_dual_writes_receipt`, and recovery-dwell test prove dry run plus exactly one real dispatch and no same-window second mutation |
| T-010 downstream-first/aging | PASS | classification/selection/aging/priority-miss tests plus `test_aging_never_bypasses_estop_manual_hold_or_host_cap` |
| T-011 pre-command races | PASS (isolated) | fingerprint/config/worker/DB-count drift aborts; multi-row dry run is `precommand-race`; unchanged fences pass the same independently computed `m` to dry and real commands |
| T-012 post-command ambiguity | PASS (isolated) | exact ordered CLI/new `(task, run, worker identity)` set; multi-start success and priority first row; malformed/duplicate/too-many rows; manual/run drift; host and stricter-profile post-cap violations; persistent uncertainty; dual-written journal/status and fresh dwell |
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
| T-025 worker start-fingerprint identity (REQUIREMENT_LEDGER §18) | PASS | `test_worker_identity` (34 tests: composite parse, golden `'|179027411681'` value, epoch port incl. partial-read cases, Linux `/proc` field-22 parity, Darwin centisecond scaling, exact-string match/no tolerance, `require_consistent` tasks-vs-runs conflict); `test_board_inventory`/`test_inventory`/`test_runtime` updated fixtures; `tests/test_contract_gate.py` + `scripts/contract_gate.py` mandatory (fails, not skips, without `HERMES_SOURCE_ROOT`) end-to-end real-child-process proof against pinned Hermes `_process_fingerprint`; 9/9 hand-crafted mutants killed, plus 9 more after review round 1 (`tests/test_processes.py`, `tests/test_contract_gate_script.py`; see REQUIREMENT_LEDGER §18) |

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
| Completion race starts extras | exact identities/caps reconcile, additive receipt is written, dwell restarts; otherwise persistent uncertainty | `test_completion_race_reconciles_all_identities_and_dual_writes_receipt`, `test_stricter_profile_and_host_caps_are_verified_after_dispatch`, `test_post_difference_pairs_task_run_and_worker_identity` |
| Stop with command/worker/unknown descendant | lifecycle refusal; no bootout promise | `test_stop_refuses_commands_workers_and_unknown_descendants` |

## Reproducible verification command

From `contrib/kanban-resource-controller` in the isolated worktree:

    /Users/jhaynes/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
    /Users/jhaynes/.hermes/hermes-agent/venv/bin/python -m compileall -q -j 1 resource_controller tests
    /usr/bin/plutil -lint launchd/ai.hermes.kanban-resource-controller.plist

The external package receipt records the exact frozen SHA/tree, archive SHA-256, per-file manifest, final count/output, and cumulative `git diff --check` receipt. Tests are serial at the file runner level; only finite child execution inside the timeout case provides controlled concurrency.

## Configurable-cap mutation receipt

Disposable copies were made from the implementation tree; each row records the disposable-tree SHA-256, the behavior test run, and its actual nonzero exit. No test reads source text. Full receipt: `/Users/jhaynes/.hermes/profiles/builder/cache/scratch/controller-cap-mutation-receipt.txt`.

| Mutant | Killer | Exit | Disposable tree SHA-256 |
|---|---|---:|---|
| `_common_hold` host literal 2 | `test_nondefault_common_host_cap_uses_the_shared_caps_object` | 1 | `530b2ddec45d171a2dabaef73e300e8708f9470eb8b500f5d7813e49fa415a2d` |
| inventory host literal 2 | `test_capacity_is_host_profile_and_board_admission_only` | 1 | `6012d46de77a356de157b3cc8e441a3f2b2fcffd5837a34f1d3c4ddca4523d2b` |
| ignore profile override | same capacity boundary test | 1 | `a8a49403fabe873d6a0b5c73403946a1fb2cab209d1a52f3df9c3fea1c86d017` |
| ignore board override | same capacity boundary test | 1 | `0f66a4f7547753fe64d58ff358595ad1dabea377a07b6e9056b5fbd5f5306698` |
| hard-code dispatch `m = 1` | `test_dispatch_maximum_uses_selected_board_database_running_count` | 1 | `675f0183f68d9c80fb04019210f79f5b5851e30602aae3ad703c6c111c21f094` |
| hard-code dispatch `m = board_cap` | same `m` boundary test | 1 | `58fad5ee371469fc959a6e74f149e2b89f919086f685fb3ad73a159d7a171311` |
| use reconciled live runs for `m` | runtime count-divergence test | 1 | `3a8f8517ad448f31b17c25686974813666ba2cb8738e5a096a39f0549ebded55` |
| count all task rows for `m` | board DB-running predicate test | 1 | `d934e5c1519119d18674a71323b16d0deee9f8d42154df5afd9a7207981a58cf` |
| accept multi-row dry run | exact-one prediction test | 1 | `f5782feb2881b15fafce749a9bc7e4b06c0838550652968e7c52f5bc4ade18b1` |
| weaken new-worker set equality | task/run/worker difference test | 1 | `625997e17f1a2d26880698c49cf2d7757ee9c5c67093b307614f89b1b314cdd9` |
| omit run id from identity | same difference test | 1 | `ca74b87e4e24ce28fb1b09dcce39bc36a54103cce7692a86cf1689ce3e6a3168` |
| skip profile post-recount | stricter-profile/host race test | 1 | `d4c3b4217536696e26be09fae1731c1f95fac6e4d6019072fdfb2283b7381a07` |
| skip `command_consumed` | multi-start receipt/dwell test | 1 | `69686f01731d16792eb34be9414093be848fe3dd912d894f98de7b8e785d3ba5` |
| weaken exact Hermes cap equality | config mismatch table | 1 | `02d78f7ba2c8bdc05c9cc799f01ed479475eb85a7f1313cd832cee0fcac290a1` |
| change absent host/profile/board default | absent-section migration test (three independent copies) | 1 each | `97d141ca6c10977cf13d375d63740aa2d97c6a4715385f4e071bfae1c9cb028a`, `c22498d23b67507555a5391b1f8c7c0e16e8846a75be0edc42f1928b8433bae5`, `800fe169cf6af03a282818cc48170d7d762cf8f2a1e1dc2c11963e0a1d88b5fe` |

## Open activation/release requirements

- B-003: production stale-timeout policy is expected to be incompatible until explicitly approved/changed or supported CLI parity exists.
- B-004: real Darwin launchd coalition canary is not authorized and not performed.
- B-005: live read-only unowned-subscription recheck is not performed by this build task.
- B-006: the implementation snapshot `4d081104d8039869ae64c74a202575ca2e2924e8` received exact-SHA quality and mandatory cross-company `reviewscope` approval. The final packaging snapshot must receive the same exact-SHA review before release handoff.
- Real launchd topology, live read-only telemetry, production board metadata, and notification routing remain release gates; fixture or sandbox success is not represented as live proof.
- No installation, config/service mutation, activation, production dispatch/decomposition, package removal, ESTOP change, or worker signal occurred.
