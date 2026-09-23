# External Kanban resource controller

Status: reviewable staging artifact only. It is not installed, configured, loaded, or active.

This standalone Python helper gates automatic Kanban admission without changing Hermes source. It owns the same dispatcher flock for its active lifetime, samples Darwin host pressure every 30 seconds, reads canonical board state in SQLite read-only mode, reconciles exact worker identities, and invokes only supported Hermes CLI commands. It never writes or clears Hermes ESTOP, never signals a worker, and never repairs board rows.

## Safety model

- Hold immediately for load1 >= logical cores, non-normal native pressure, available memory below 4 GiB, either paging direction, malformed telemetry, first sample, counter reset, time reversal, or a sample gap.
- Recover only after 120 continuous seconds at load1 <= 0.8 cores, at least 5 GiB available, normal pressure, and no paging.
- Enforce host admission cap 2, per-profile cap 1, and per-board `dispatch --max 1` after exact process/run reconciliation. Existing excess workers drain naturally.
- Perform at most one side-effecting command in a recovered window: one decomposition or one dispatch. Every attempted command consumes the window.
- Predict each board with supported `dispatch --dry-run --max 1`; classify merge-conflict > review > build; use round-robin ties; age a passed board into one admission after six windows. The accepted race is reported as `priority_miss`; policy is explicitly best-effort.
- Persist a pending journal before command launch. Timeout, output drift, nonzero exit, launch failure, or post-command disagreement remains an operator hold across restart.
- Refuse active behavior unless the effective default config is explicitly compatible, including `dispatch_in_gateway: false`, `max_in_progress: 2`, per-profile cap 1, orphan reconciliation on, and stale timeout 0.
- Any null/blank notification owner on any board is an activation hold. Historical zero counts are not cached or promised.

## Repository staging layout

- `resource_controller/`: standalone implementation; no imports from Hermes internals.
- `tests/`: deterministic serial unit/contract tests.
- `controller.example.json`: uninstalled runtime configuration template.
- `launchd/ai.hermes.kanban-resource-controller.plist`: uninstalled template only.
- `RUNBOOK.md`: separately gated installation, lifecycle, canary, and rollback.
- `REQUIREMENT_MATRIX.md`: requirement-to-test coverage and open gates.

## Serial development gates

From this directory, using the retained Hermes Python environment:

    PYTHONPATH=. python3 -m unittest discover -s tests -v
    python3 -m compileall -q -j 1 resource_controller tests

Do not run these tests in parallel. They use temporary homes/databases and finite synthetic subprocesses only. No test dispatches production work.

## CLI surface

    python3 -m resource_controller.main status --config /absolute/controller.json
    python3 -m resource_controller.main check  --config /absolute/controller.json
    python3 -m resource_controller.main hold   --config /absolute/controller.json --reason operator
    python3 -m resource_controller.main resume --config /absolute/controller.json --reason operator
    python3 -m resource_controller.main run    --config /absolute/controller.json

`check` is observation-only and does not take the dispatcher lock. `run` is active and MUST NOT be invoked until every release gate in `RUNBOOK.md` is approved. `resume` clears only the helper's own manual hold and never touches Hermes ESTOP.
