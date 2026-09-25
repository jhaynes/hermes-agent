# External Kanban resource controller

Status: reviewable staging artifact only. It is not installed, configured, loaded, or active.

This standalone Python helper gates automatic Kanban admission without changing Hermes source. It owns the same dispatcher flock for its active lifetime, samples host memory pressure every 30 seconds via a cross-platform (Darwin/Linux) telemetry backend, reads canonical board state in SQLite read-only mode, reconciles exact worker identities, and invokes only supported Hermes CLI commands. It never writes or clears Hermes ESTOP, never signals a worker, and never repairs board rows.

## Safety model

- Hold immediately for load1 >= logical cores, non-normal native pressure, available memory below 4 GiB, a swap-out counter increase, malformed/unreadable telemetry, first sample, counter reset, time reversal, or a sample gap. Swap-in growth alone never holds (D1 = A).
- Recover only after 120 continuous seconds at load1 <= 0.8 cores, at least 5 GiB available, normal pressure, and no swap-out increase.
- Read host, profile, and board blanket caps plus named overrides once from `controller.json.admission`. The sole backward-compatible defaults are 2/1/1 in `spec.py`; existing excess workers drain naturally.
- Perform at most one mutating command in a recovered eligibility window: one decomposition or one dispatch. Read-only dry runs do not count. Every attempted mutating command consumes the window.
- Predict each board with supported `dispatch --dry-run --max <m>` and run the one real dispatch with the same fenced `m = min(effective board cap, Hermes database running count + 1)`. Normal operation leaves one slot. Completion races may report extras, accepted only after exact task/run/worker reconciliation and post-command cap verification.
- Persist a pending journal before command launch. Timeout, output drift, nonzero exit, launch failure, or post-command disagreement remains an operator hold across restart.
- A worker that ends in Hermes while its process is still exiting remains countable only when the immediately preceding successful capture, the current process fingerprint/argv/environment, and the exact retained ended `task_runs` row all agree. A fresh controller has no prior identity and holds fail-closed.
- Every uncertain command path stores one sanitized, single-line, 300-character `{type,message}` diagnostic in the pending journal and status; transition logs carry the same bounded cause.
- `controller.json.alerting` configures the stuck delay and notification timeout, with both defaults declared once in `spec.py`. Each unbroken `uncertain-outcome` or `persistent-operator-hold` incident gets at most one notification attempt: the controller records the attempt, writes the guaranteed `controller.log` line, then tries local Notification Center on macOS. Alerting never acknowledges uncertainty or resumes admission.
- Refuse active behavior unless the effective default config is explicitly compatible, including `dispatch_in_gateway: false`, Hermes host cap equal to the controller host cap, Hermes global profile cap equal to the largest effective controller profile cap, an explicit nonempty `dispatch_profiles` registry, orphan reconciliation on, and stale timeout 0.
- Any null/blank notification owner on any board is an activation hold. Historical zero counts are not cached or promised.

## Repository staging layout

- `resource_controller/`: standalone implementation; no imports from Hermes internals.
- `resource_controller/telemetry/`: per-OS memory/pressure backend (`darwin.py`, `linux.py`), the shared `MemoryHealth`/`TelemetryError` contract, and named constants (`__init__.py`, `constants.py`).
- `resource_controller/alerting.py`: durable at-most-one-attempt stuck-incident tracking, independent of command journals.
- `tests/`: deterministic serial unit/contract tests.
- `controller.example.json`: uninstalled runtime configuration template.
- `launchd/ai.hermes.kanban-resource-controller.plist`: uninstalled template only.
- `RUNBOOK.md`: separately gated installation, lifecycle, canary, and rollback.
- `REQUIREMENT_MATRIX.md`: requirement-to-test coverage and open gates.
- `PACKAGE_MANIFEST.md`: complete packaged-file/change inventory and immutable identity model.

## Serial development gates

From this directory, using the retained Hermes Python environment:

    PYTHONPATH=. python3 -m unittest discover -s tests -v
    python3 -m compileall -q -j 1 resource_controller tests

Do not run these tests in parallel. They use temporary homes/databases and finite synthetic subprocesses only. No test dispatches production work.

The packaged archive and external release receipt are produced only from a clean frozen commit after these gates pass. See `PACKAGE_MANIFEST.md`; the archive remains uninstalled and unactivated.

## CLI surface

    python3 -m resource_controller.main status --config /absolute/controller.json
    python3 -m resource_controller.main check  --config /absolute/controller.json
    python3 -m resource_controller.main hold   --config /absolute/controller.json --reason operator
    python3 -m resource_controller.main resume --config /absolute/controller.json --reason operator
    python3 -m resource_controller.main run    --config /absolute/controller.json

`check` is observation-only and does not take the dispatcher lock. `run` is active and MUST NOT be invoked until every release gate in `RUNBOOK.md` is approved. `resume` clears only the helper's own manual hold and never touches Hermes ESTOP.
