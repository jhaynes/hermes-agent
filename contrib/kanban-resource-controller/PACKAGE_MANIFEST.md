# Package manifest

## Identity model

The immutable deliverable is identified by all of the following in the external release receipt produced from the clean frozen commit:

- frozen Git commit and tree;
- original comparison base `5910de20bc9839fdd36e791a9d72ba2c2e722f66`;
- deterministic archive SHA-256 and byte size;
- SHA-256 and byte size for every file in the archive;
- exact serial verification commands and outcomes executed at the frozen commit.

The release receipt is intentionally outside the archive: an archive cannot contain its own final digest without a circular identity. Verify the receipt and archive before extraction. `git archive` includes tracked files only; runtime state, credentials, production configuration, databases, logs, and worktrees are excluded.

Reproducible package command, with `<frozen-sha>` replaced by the reviewed packaging commit. The explicit commit timestamp is required because archiving the `commit:path` tree expression otherwise stamps entries with wall-clock time:

    frozen_sha=<frozen-sha>
    frozen_mtime=$(git show -s --format=%ct "$frozen_sha")
    git archive --format=tar --mtime="@$frozen_mtime" \
      --prefix=kanban-resource-controller/ \
      "$frozen_sha":contrib/kanban-resource-controller \
      | gzip -n > kanban-resource-controller-"$frozen_sha".tar.gz

The archive is a review artifact only. Its existence is not installation or activation authorization.

## Cumulative change boundary

All cumulative changes from the original base are confined to `contrib/kanban-resource-controller/`. No installed Hermes source, runtime configuration, service, production board, ESTOP, or package is included or modified by the artifact.

The configurable-cap implementation base is the installed controller `7771e842390d627baa99e1ceee026f2ef20512cd` (tree `101b2e94814eafda322c84e19e874cf7aa36546f`). The external receipt records the final SHA, tree, package-only cumulative diff, and exact file list.

## Complete packaged file inventory

### Operator documentation and templates

- `README.md` — staging status, safety model, CLI surface, and verification entry points.
- `REQUIREMENT_LEDGER.md` — binding contract, decisions, test plan, and blocker provenance.
- `REQUIREMENT_MATRIX.md` — final requirement-to-test/failure-path mapping and release gates.
- `RUNBOOK.md` — future-authorized installation, operation, canary, shutdown, and rollback procedure.
- `PACKAGE_MANIFEST.md` — package identity, full content/change manifest, and exclusions.
- `controller.example.json` — uninstalled runtime configuration template with placeholders.
- `launchd/ai.hermes.kanban-resource-controller.plist` — uninstalled LaunchAgent template.

### Standalone implementation

- `resource_controller/__init__.py`
- `resource_controller/alerting.py` — durable one-shot stuck-incident state using bounded telemetry diagnostics
- `resource_controller/board_inventory.py`
- `resource_controller/cli_adapter.py`
- `resource_controller/cli_contract.py`
- `resource_controller/config.py`
- `resource_controller/engine.py`
- `resource_controller/host.py`
- `resource_controller/inventory.py`
- `resource_controller/lifecycle.py`
- `resource_controller/locking.py`
- `resource_controller/main.py`
- `resource_controller/policy.py`
- `resource_controller/preflight.py`
- `resource_controller/priority.py`
- `resource_controller/processes.py`
- `resource_controller/runtime.py`
- `resource_controller/spec.py`
- `resource_controller/storage.py`
- `resource_controller/supervision.py`
- `resource_controller/telemetry/__init__.py` — `MemoryHealth`/`TelemetryError` contract and `select_backend`
- `resource_controller/telemetry/constants.py` — named reason/source/pressure/schema-version/error constants
- `resource_controller/telemetry/darwin.py` — real `vm_stat` Swapins/Swapouts parser and backend (no `psutil.swap_memory`)
- `resource_controller/telemetry/linux.py` — `/proc/vmstat`, `/proc/meminfo`, `/proc/pressure/memory` parser and backend
- `resource_controller/worker_identity.py` — single owner of the pinned Hermes worker start-fingerprint contract (composite-only parse, exact-string match, `IdentityHold` subclasses)

### Serial deterministic tests

- `tests/test_alerting.py`
- `tests/test_board_inventory.py`
- `tests/test_cli_adapter.py`
- `tests/test_config_cli.py`
- `tests/test_engine.py`
- `tests/test_host_lifecycle.py`
- `tests/test_inventory.py`
- `tests/test_live_cli_contract.py`
- `tests/test_lock_storage_supervision.py`
- `tests/test_main.py`
- `tests/test_old_reader_compat.py` — mandatory exact-archive rollback/config/additive-field compatibility gate when `CONTROLLER_OLD_ARCHIVE` is set
- `tests/test_policy_priority.py`
- `tests/test_preflight.py`
- `tests/test_runtime.py`
- `tests/test_sandbox_cli_e2e.py`
- `tests/test_spec.py`
- `tests/test_telemetry.py` — Darwin/Linux parser, backend selection, PSI threshold, telemetry-error, and end-to-end divergent-source regression coverage
- `tests/test_worker_identity.py` — worker start-fingerprint contract: composite parse, golden value, epoch port, Linux/Darwin start-time parity, exact-string match
- `tests/test_contract_gate.py` — mandatory release-gate: real child process, fingerprint written by pinned Hermes `_process_fingerprint`, verified against pinned commit
- `scripts/contract_gate.py` — mandatory pre-install gate script; fails (not skips) if `HERMES_SOURCE_ROOT` is unset
- `scripts/old_reader_gate.py` — mandatory exact 9b0b4f26 rollback-reader gate; fails if the archive is missing, wrong, skipped, or incompatible
- `tests/test_processes.py` — start-fingerprint bracketing of the environment read, recapture, `identity-unstable`
- `tests/test_contract_gate_script.py` — the gate script's own control logic (missing env, HEAD pin, skips, empty/failed runs)
- `tests/fixtures/vm_stat_darwin_16k.txt` — real captured `vm_stat` output, 16 KiB pages
- `tests/fixtures/vm_stat_darwin_4k.txt` — 4 KiB-page variant
- `tests/fixtures/proc_vmstat.txt` — real captured `/proc/vmstat`
- `tests/fixtures/proc_meminfo.txt` — real captured `/proc/meminfo`
- `tests/fixtures/proc_pressure_memory.txt` — real captured `/proc/pressure/memory`

## Change manifest by acceptance requirement

| Acceptance requirement | Packaged change/evidence |
|---|---|
| Supported external helper, no Hermes core change | Standalone `resource_controller/` implementation and cumulative path-boundary receipt |
| Incompatible-config refusal | `config.py`, `preflight.py`, `spec.py`, templates, refusal tests, and runbook contract |
| Singleton gateway coordination | `locking.py`, engine/runtime integration, singleton tests, and runbook coordination sequence |
| Admission/hold/recovery/caps/priority | `policy.py`, `priority.py`, `engine.py`, inventory modules, and mapped serial tests |
| Lifecycle/launchd descendants and safe shutdown | `lifecycle.py`, process/supervision modules, state-boundary tests, plist template, and B-004 runbook gate |
| Timeout/race/uncertainty behavior | CLI/supervision/runtime modules, pending-journal storage, and failure-path matrix |
| Legacy unowned delivery limitation | board inventory/preflight behavior plus explicit historical-only warning in ledger, matrix, and runbook |
| Rollback and bounded canary | lifecycle/storage behavior, template boundaries, explicit authorization phases and abort criteria in runbook |
| Immutable package and reproducible evidence | this manifest plus external release receipt/archive hash generated from and tested at the frozen commit |
| Cross-platform memory telemetry (REQUIREMENT_LEDGER §17) | `resource_controller/telemetry/` package, `host.py`/`policy.py`/`spec.py`/`main.py`/`engine.py` updates, `tests/test_telemetry.py`, real Darwin/Linux fixtures |
| Worker start-fingerprint identity (REQUIREMENT_LEDGER §18) | `resource_controller/worker_identity.py`, `board_inventory.py`/`inventory.py`/`processes.py`/`runtime.py`/`main.py` updates, `tests/test_worker_identity.py`, mandatory `scripts/contract_gate.py` + `tests/test_contract_gate.py` against pinned Hermes |
| Configurable admission caps and C′ dispatch (REQUIREMENT_LEDGER §19) | `spec.py` sole 2/1/1 defaults and strict admission parser; exact Hermes/profile-registry checks; selected-board DB-running count; explicit dry/real `m`; exact multi-start identity/cap reconciliation; additive rollback-compatible receipts; check output; docs/template; behavior and 17-mutant receipts |
| Finishing-worker resilience, diagnostics, alerts (REQUIREMENT_LEDGER §20) | bounded exact ended-run lookup plus process-local prior successful identity; unchanged `LiveWorker` dispatch identity/capacity/drain behavior; additive sanitized uncertainty fields and legacy fallback; strict alert delay/notify-timeout config with sole `spec.py` defaults; at-most-one log/Notification Center attempt; RED and 31-mutant receipts |

## Configurable-cap TDD and mutation evidence

- Frozen contract inputs: plan SHA-256 `3edce15d0305d4e067a028a06aab5f7d0bc1346b8f66b41b7fc04b116cd1cf3f`; addendum SHA-256 `92454021d4d5ae73dabc02d9562dc62a14b4fa7e92e928f37ff5f9a7ae200fbf`; original base `7771e842390d627baa99e1ceee026f2ef20512cd`.
- Proven-red slices against that base: admission absence/non-default parsing failed with missing field/unknown key; config caps API and explicit profile registry failed; configured inventory and dispatch-maximum helpers were absent; board snapshots lacked `hermes_db_running_count`; explicit dry/real `m=2` failed because builders hard-coded `1`. Exact commands/results are summarized in REQUIREMENT_LEDGER §19.
- Mutation campaign: 17/17 disposable mutants exited nonzero. It kills host literals in both hold paths, old profile/board-any behavior and ignored overrides, four wrong `m` sources/values, multi-row dry-run acceptance, weakened task/run/worker set identity, skipped post-profile recount, skipped dwell consumption, weakened Hermes equality, and each changed absent-section default. The complete mutant/test/exit/tree-hash table is in `REQUIREMENT_MATRIX.md`; raw receipt is `/Users/jhaynes/.hermes/profiles/builder/cache/scratch/controller-cap-mutation-receipt.txt`.
- Review round 1 follow-up: four behaviour tests close mutants (h), (i), (k1/k2) and (x) at their operative layers. REQUIREMENT_MATRIX has the table.
- Final macOS/Linux serial counts, contract-gate result, frozen commit/tree, and deterministic archive digest are written here immediately before freeze.

## Finishing-worker TDD and verification evidence

- Frozen contract inputs: plan SHA-256 `cf719287ed03e219ff8f16f7603395abd0878489823412ab56c3134ddfc62c9d`; implementation base `d25d1dd82edb8ae66d8ddee400b0a90e5b9f7bbb` (tree `2c38abfc863506ac0eacf40808ff5e20fffebdff`).
- Proven-red slices: ended-candidate API absent; second capture held no-canonical; the real post-dispatch reproduction returned uncertainty; result diagnostics/config fields were absent; alerting module was absent. Exact tests and observed failures are in `REQUIREMENT_MATRIX.md`.
- Addendum 1 input: SHA-256 `acb1252f9d370701da21ca7e4ebc3b9cb797dd7b26d0a3472c27ac7ce9ec64d1`; active-worker consecutive capture, clock regression, configurable notify timeout, rollback rejection, additive status wording, and alert/crash semantics are included.
- Safety characterization: base cases that already held are identified separately, not represented as RED. The completed 31/31 mutation campaign kills the addendum's mandatory core plus defence-in-depth mutants; exact killer/exit/tree hashes are in `REQUIREMENT_MATRIX.md`, with raw JSON at `/Users/jhaynes/.hermes/profiles/builder/cache/scratch/controller-finishing-mutation-results.json`.
- Old-reader: mandatory `scripts/old_reader_gate.py` against exact archive SHA-256 `5709b8ccd7a6125ec2c52c76711a6553afef65344d26e4f18feb672885265bef` proves 9b0b4f26 rejects `alerting`, reads additive journal/status fields, returns pending=true and reconciled=false.
- Pre-freeze macOS gate: 242 run, 239 passed, 0 failed, 3 expected skips (two pinned-source tests and one exact-archive test); compileall and plist lint pass. Mandatory macOS contract gate: 2/2 pass against pinned Hermes `0e0a29ad315da6b6fd5b63e2903600af85e839e5`; mandatory old-reader gate: 1/1 pass.
- Linux clean-copy gate: 242 run, 239 passed, 0 failed, 3 expected external-input skips, using immutable `python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`, `psutil==7.2.2`, and `PyYAML==6.0.3`.
- `scripts/run_tests.sh contrib/kanban-resource-controller/tests` is not a valid package gate from repository root because its per-file pytest subprocesses do not put this standalone package directory on `PYTHONPATH`; all package files fail collection with `ModuleNotFoundError: resource_controller`. The binding package gate is the plan/task's serial `unittest` command run from the package directory. No out-of-scope repository test-runner change was made.

## Explicit exclusions

The package contains no credentials, runtime state, SQLite database, production config receipt, service backup, live telemetry receipt, lock file, manual-hold file, pending journal, log, Python environment, installed Hermes source modification, load-adaptive branch content, Smithers #2041 work, or production-canary evidence.

No production activation or production canary occurred while producing this package.
