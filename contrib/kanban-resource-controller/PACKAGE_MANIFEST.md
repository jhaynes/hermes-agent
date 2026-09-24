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

The predecessor implementation/verification snapshot is `4d081104d8039869ae64c74a202575ca2e2924e8` (tree `f94c7def18de02b772dab29dd487482074a8581c`). The packaging snapshot adds/updates only operational documentation and package metadata; the external receipt records its final SHA, tree, cumulative diff stat, and exact file list.

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

- `tests/test_board_inventory.py`
- `tests/test_cli_adapter.py`
- `tests/test_config_cli.py`
- `tests/test_engine.py`
- `tests/test_host_lifecycle.py`
- `tests/test_inventory.py`
- `tests/test_live_cli_contract.py`
- `tests/test_lock_storage_supervision.py`
- `tests/test_main.py`
- `tests/test_policy_priority.py`
- `tests/test_preflight.py`
- `tests/test_runtime.py`
- `tests/test_sandbox_cli_e2e.py`
- `tests/test_spec.py`
- `tests/test_telemetry.py` — Darwin/Linux parser, backend selection, PSI threshold, telemetry-error, and end-to-end divergent-source regression coverage
- `tests/test_worker_identity.py` — worker start-fingerprint contract: composite parse, golden value, epoch port, Linux/Darwin start-time parity, exact-string match
- `tests/test_contract_gate.py` — mandatory release-gate: real child process, fingerprint written by pinned Hermes `_process_fingerprint`, verified against pinned commit
- `scripts/contract_gate.py` — mandatory pre-install gate script; fails (not skips) if `HERMES_SOURCE_ROOT` is unset
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

## Explicit exclusions

The package contains no credentials, runtime state, SQLite database, production config receipt, service backup, live telemetry receipt, lock file, manual-hold file, pending journal, log, Python environment, installed Hermes source modification, load-adaptive branch content, Smithers #2041 work, or production-canary evidence.

No production activation or production canary occurred while producing this package.
