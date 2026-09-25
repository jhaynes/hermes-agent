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
| T-026 finishing worker / bounded uncertainty / stuck alert (REQUIREMENT_LEDGER §20) | PASS (isolated) | Exact retained ended-run + prior/current process proof; restart/no-prior and all mismatch holds; real post-dispatch race; same worker identity and capacity/drain accounting; sanitized journal/status/log diagnostics and legacy fallback; strict configured alert delay, durable one-shot restart behavior, static Darwin argv and Linux log-only path. See the RED and 27-mutant receipts below. |

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


**Review round 1 follow-up (desktop, 2026-09-24).** reviewtests (deleg_039316e0) re-ran the campaign. Mutants (h) and (x) were killed in the table above only at their helper or incidental layers: (h) is the all-boards count at the engine real-dispatch call site, and (x) is null/empty `dispatch_profiles` with NO profile overrides, which is the production template. Mutants (i) (board-at-DB-cap falls through as `--max 0`) and (k) (dry-run row with wrong assignee or ineligible profile accepted) survived. Four behaviour tests were added, and every mutant now exits nonzero in a disposable copy:

| Mutant | Killer | Exit |
|---|---|---:|
| (h) engine real-dispatch `m` from all boards' DB-running count | `test_real_dispatch_max_uses_only_the_selected_board_database_count` | 1 |
| (i) final-fence `dispatch_max is None` falls through as `--max 0` | `test_final_fence_database_board_cap_issues_no_command` | 1 |
| (k1) dry-run row assignee differs from fenced inventory | `test_prediction_row_must_match_fenced_assignee_and_eligible_profiles` | 1 |
| (k2) dry-run row profile not dispatch-eligible | same | 1 |
| (x) null/empty `dispatch_profiles` with no profile overrides | `test_empty_profile_registry_is_refused_without_profile_overrides` | 1 |

The reviewsystem low finding (the in-tree old-reader test exercises the current reader, not the installed 7771e842 binary) is recorded as a release-procedure check instead: RUNBOOK install step "old-reader check" runs the extracted previous archive's `SecureStateStore.has_pending_uncertainty` against the live journal before a binary swap. The review ran that probe live: reconciled multi-start is non-blocking, and pending/uncertain is blocking.

## Finishing-worker TDD, compatibility, and mutation receipt

Binding plan SHA-256: `cf719287ed03e219ff8f16f7603395abd0878489823412ab56c3134ddfc62c9d`; binding Addendum 1 SHA-256: `acb1252f9d370701da21ca7e4ebc3b9cb797dd7b26d0a3472c27ac7ce9ec64d1`. Exact base/tree: `d25d1dd82edb8ae66d8ddee400b0a90e5b9f7bbb` / `2c38abfc863506ac0eacf40808ff5e20fffebdff`.

| Proven-red slice on base | Exact test | Observed base result |
|---|---|---|
| retained ended run projection | `test_ended_run_projection_requires_ended_at_and_retained_exact_identity` | API rejected `ended_candidates` |
| normal second capture | `test_second_capture_keeps_just_ended_worker_counted` | `IdentityHold: worker process 42 has no canonical run` |
| post-dispatch just-ended worker | `test_post_dispatch_capture_tolerates_unrelated_just_ended_worker` | `uncertain-outcome`, expected `dispatched` |
| structured uncertainty | `test_uncertain_command_persists_and_blocks_restart` | `CommandOutcome` rejected `error=` |
| configured alert delay | `test_alert_delay_defaults_and_configured_value_reach_runtime_spec` | `RuntimeSpec` lacked `stuck_alert_after_seconds` |
| alert tracker | `test_alerting.StuckAlertTrackerTests` | `resource_controller.alerting` absent |

Safety-characterization cases (no prior identity, mismatches, exact board/profile/unique PID, stopping) already held on base where applicable; the mutation campaign proves the narrow exception did not widen them. The actual extracted `9b0b4f26` reader returned `True` for a new pending journal with additive `error` and `False` for reconciled with additive `error`.

All mutations ran the complete suite in a disposable copy. The Addendum 1 mandatory core is M01/M02/M03/M05/M09/M10/M15/M17; every other row is defence-in-depth. Raw receipt: `/Users/jhaynes/.hermes/profiles/builder/cache/scratch/controller-finishing-mutation-results.json`.

| Mutant | Exact killer | Exit | Disposable tree SHA-256 |
|---|---|---:|---|
| `M01-no-prior-guard` | `test_ended_worker_without_previous_capture_holds` | 1 | `6a2f157659019616595577bcc0d04f76cefeb2345e7fb5443a694c39993ce7a3` |
| `M02-ignore-ended-at` | `test_consecutive_active_captures_do_not_create_ended_evidence_or_hold` | 1 | `f7e0ccf5beb37d605df64bcfb447204d11920871f060e41ff5407fde6f9a87d7` |
| `M03-bypass-fingerprint` | `test_previous_identity_mismatch_or_unended_run_holds` | 1 | `676713cc939f4d0d47826116f1d706d1cc7329214bd262603c415c5e32394f30` |
| `M04-bypass-argv` | `test_previous_identity_mismatch_or_unended_run_holds` | 1 | `02440900cb40ae5e33f98a6ea84e37694af64fb0b1fa8a1d7a96712cc19bcb1e` |
| `M05-bypass-environment` | `test_finishing_worker_requires_board_profile_unique_pid_and_every_marker` | 1 | `e8a473bab0f0a9d503b5c2c8dfd51d5994a60466e939aed185550370a064807f` |
| `M06-persist-prior-across-runtime` | `test_previous_identity_is_not_shared_with_a_fresh_runtime_world` | 1 | `a5bdbdd5f232eb0f03c3488024f5601658da9562c7d96e5c58f30facbf225cb6` |
| `M07-omit-finishing` | `test_second_capture_keeps_just_ended_worker_counted` | 1 | `c6298c19e359e83c62ba13c4b340d9e73ce7880b5c68d76916780b30b521eee2` |
| `M08-status-in-dispatch-identity` | `test_post_dispatch_capture_tolerates_unrelated_just_ended_worker` | 1 | `73a435625ff9c80acdbcd6ac99465c1633e5a512a965481e2d8a2c9d51cec756` |
| `M09-publish-cache-early` | `test_failed_capture_does_not_poison_previous_identity_cache` | 1 | `e8f437e749bc3d3fca4c566607e2ef8962a6b4356703a5de34499563cd1c9f7a` |
| `M10-drop-journal-error` | `test_each_engine_uncertainty_path_persists_bounded_reason` | 1 | `3be571b000c8ececeff309ada0a3ddc1c0d30b29bded8942528c8f24fa0178bd` |
| `M11-drop-status-error` | `test_uncertain_command_persists_and_blocks_restart` | 1 | `ea8f2e05a0a10bfbf8fb51eb02082085f8552ba3c9c9dba5dc89bcb54ca49fa1` |
| `M11b-drop-log-error` | `test_logs_starts_and_reason_changes_only` | 1 | `5cd8ac47e1ecb738090eded88a93788efd7b0666a3903686e884c3d4fd8da6a0` |
| `M12-bypass-sanitizer` | `test_uncertain_command_persists_and_blocks_restart` | 1 | `96844f64e496f4fb08b33402a1946aa71498da557aa2511908ba420cbd0a3bf2` |
| `M13-hardcode-alert-delay` | `test_configured_alert_threshold_controls_runtime_observation` | 1 | `2cb28301dcba9ab84901620f1e765ec8c3b06c6bd47ddb476cd03578282de208` |
| `M13b-hardcode-notify-config` | `test_configured_alert_threshold_controls_runtime_observation` | 1 | `00a266f43f8890a7524edd09cd13a670f6f84028a429792307ed09964c5fb43d` |
| `M14-threshold-exclusive` | `test_threshold_boundary_details_changes_and_restart_are_one_incident` | 1 | `11cf0bbc1f2dc11f3d60d4470dfcbac569154af3f81dcdbd60455bf9694cafc0` |
| `M15-remove-one-shot` | `test_threshold_boundary_details_changes_and_restart_are_one_incident` | 1 | `c00a10bd9188129db03a3277dba12c8fd7a36d46c0e12609f2db8b11c31b4381` |
| `M16-alert-nonstuck` | `test_normal_status_resets_incident_and_nonstuck_reasons_never_alert` | 1 | `33dec4a2c073f9ef97c197b2f8658816ce7baac3f35fd45ec465406d611d1209` |
| `M17-alert-acks-pending` | `test_callback_failure_is_attempted_once_and_malformed_state_is_fail_safe` | 1 | `55ce12fef897323e19d514a55273c061e105f9894fa71a4c40f1053a8879bca9` |
| `M18-interpolate-applescript` | `test_darwin_notifier_uses_static_argv_no_shell_and_spec_timeout` | 1 | `b92fab9458eafcff092d666bea746c19fdd8d277a32d349203787e201c2ba727` |
| `M18b-drop-pre-notify-log` | `test_darwin_notifier_uses_static_argv_no_shell_and_spec_timeout` | 1 | `35d19c9a126a2803547d510aec5d49b4ee155564d2d028715a29bc47c81615a0` |
| `M19-accept-duplicate-prior-pid` | `test_finishing_worker_requires_board_profile_unique_pid_and_every_marker` | 1 | `d9a268ca5ab6e2e44d6eedf5f010532048da032805cd22939248ffe3d8ba0e7e` |
| `M19-reset-on-detail-change` | `test_threshold_boundary_details_changes_and_restart_are_one_incident` | 1 | `f97b0838801debd6b01da2a723db46b47d7da66c556f323bdf7057a343787e5e` |
| `M20-malformed-state-crashes` | `test_callback_failure_is_attempted_once_and_malformed_state_is_fail_safe` | 1 | `3cfb743d32fa2c3cbb1d1958fa77cd0852d65167927d3e973b32f8b27d293dca` |
| `M21-hardcode-notifier-timeout` | `test_notification_timeout_logs_fallback_once` | 1 | `38701e04cbf25ff3e19afb2240364f5209b3a62962b8cd1274c9ce9a0d377615` |
| `M22-omit-finishing-drain` | `test_stopping_waits_for_finishing_worker_then_exits` | 1 | `cd3fe08d936269c061a05443ed820b1035644b6a074021b98978afc3ce906905` |
| `M23-rewrite-legacy-pending` | `test_legacy_pending_gets_safe_fallback_without_rewrite` | 1 | `c4cb77b78fd1466d7e71b210f2c4f9f8f68e7af178473fe8c9534e19c1ba1f0c` |
| `M24-ignore-config-alert` | `test_alert_delay_defaults_and_configured_value_reach_runtime_spec` | 1 | `559055dcba1a504f871db52d92f5ab9ae5d80ceb55115686aafcd7385bc604a1` |
| `M25-change-alert-default` | `test_alert_delay_defaults_and_configured_value_reach_runtime_spec` | 1 | `2fa19d91d6063fd95326bd4249a8e8f9e99d85cf442352c27f8dc53ac4a18293` |
| `M26-change-notify-default` | `test_alert_delay_defaults_and_configured_value_reach_runtime_spec` | 1 | `dba2604ba8a598c4b958bcaae9fc07dc01891810b78ef167eae1ff32474cbb52` |
| `M27-ignore-clock-regression` | `test_backwards_clock_resets_future_incident_start` | 1 | `069389f58ab5dee396e0815da3abf02bedebcb259e252f0c4f0851203fd8eaa0` |

Green gates before freeze: macOS 242 run / 239 pass / 0 fail / 3 expected skips; Linux (immutable `python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`, `psutil==7.2.2`, `PyYAML==6.0.3`) 242 run / 239 pass / 0 fail / 3 expected skips. Two ordinary-suite skips are the mandatory pinned-source contract tests, which passed 2/2 separately on macOS against Hermes `0e0a29ad315da6b6fd5b63e2903600af85e839e5`; the third is the exact-archive old-reader test, which passed 1/1 separately against archive SHA-256 `5709b8ccd7a6125ec2c52c76711a6553afef65344d26e4f18feb672885265bef`. Compileall and plist lint passed.

## Open activation/release requirements

- B-003: production stale-timeout policy is expected to be incompatible until explicitly approved/changed or supported CLI parity exists.
- B-004: real Darwin launchd coalition canary is not authorized and not performed.
- B-005: live read-only unowned-subscription recheck is not performed by this build task.
- B-006: the implementation snapshot `4d081104d8039869ae64c74a202575ca2e2924e8` received exact-SHA quality and mandatory cross-company `reviewscope` approval. The final packaging snapshot must receive the same exact-SHA review before release handoff.
- Real launchd topology, live read-only telemetry, production board metadata, and notification routing remain release gates; fixture or sandbox success is not represented as live proof.
- No installation, config/service mutation, activation, production dispatch/decomposition, package removal, ESTOP change, or worker signal occurred.
