# Finite implementation handoff — t_206ec88f

## Verdict and ownership

Implementation phase only: standalone helper, serial tests, native read-only
telemetry, real isolated CLI canary, operating docs and install template are
present. **Not installed, independently approved, or activated.**

Release successor: `default/t_da3d77bf`. Its pre-existing dependency on this card
is the review/release lane; no duplicate same-card review or delegate fleet was
started. The release owner must include the opposite-maker `reviewscope` reviewer
in every independent exact-snapshot review round. See `CONTRACT.md` for the
original request, provenance, hashes, non-goals and pre-edit mapping.

Original base: `5910de20bc9839fdd36e791a9d72ba2c2e722f66`.
Branch: `ops/kanban-resource-controller`.
Worktree: `/Users/jhaynes/.hermes/hermes-agent/.worktrees/t_206ec88f`.
Only `contrib/kanban-resource-controller/` is committed. The frozen commit SHA is
recorded in the native completion metadata and
`scratch/controller-build/t_206ec88f/build-receipt.json`; that avoids embedding a
self-referential commit hash in its own tree. Review the cumulative diff from the
original base, not only the final changed file.

## Acceptance ledger: callers, implementation and evidence

All changes below are `in_scope_required` under the binding task.

| Contract | Real caller / implementation | Verification |
|---|---|---|
| Native load/pressure/memory/paging policy | `controller.service_cycle` → `safe_sample` → `telemetry.sample_mac`; `Controller.tick` → `Gate.observe` | `test_policy`: thresholds, both paging directions, unknown/NaN, cold/reset/gap, 120s dwell, cooldown; `test_host`: actual Darwin sensors |
| Fresh checks before admission | `Controller.tick` rereads `Runtime.snapshot`, then resamples native resources immediately before execute | `test_engine`: fresh pressure veto after initial healthy dwell |
| One command/window; no destructive maintenance on holds | `Controller.tick` → `choose` → `Runtime.execute`; only dispatch --max1 OR decompose one ID | `test_engine`: round-robin a→b, ready wins over triage, cooldown, host/profile/board cap holds, ESTOP/manual holds; code scan confirms no --max0, --all, reclaim/kill/repair |
| Preserve live auto-decompose setting and profile isolation | `Runtime.config` → fresh source-pinned `config_probe.py` → real `load_config` | Real A→B→A temp-home config test plus live change reread; CLI decompose passes `--author auto-decomposer`, matching gateway's `decompose_task(..., author="auto-decomposer")` |
| Canonical process identity, terminal workers | `Runtime.snapshot` → `read_board` (ro SQLite) → real argparse-based `WorkerParser` → `reconcile` | `test_inventory`: exact query vs flag value, stale/PID reuse/mismatched and exec-changed argv, unowned workers, ancestry; real canary completes card while stub remains alive and confirms it still counts |
| Task/run/schema compatibility | `read_board` validates columns, active task/run correspondence, immutable spawned-event PID fingerprints; `verify_source` before inventory/action | `test_hermes`: missing schema and orphan run rejected; runtime imports and CLI use actual retained source, not test replacements |
| Notifier coupling | `read_board` checks null/blank notifier_profile | `test_hermes`: unowned-subscription hold; no ownership migration or production subscription mutation |
| Singleton and observation | `serve` takes compatible flock on existing dispatcher path; `observe` takes only private state lock | `test_storage`: contention and non-inheritable FD; real supported dispatch works while shared lock held; actual observation command does not create dispatcher lock |
| Persistent uncertainty, bounded output/time | `Command.start` fsyncs pending before Popen; `wait` bounds output/deadline, never kills; loop drains/reaps; `Controller.tick` persists pre/post reconciliation | `test_command`: real timeout child survives, no repeat, output overflow, malformed JSON and nonzero exit; `test_engine`: malformed action response, restart, unavailable inventory/reader retain uncertainty |
| Secure status/logs/config | `Store` checks ownership/modes/symlinks, atomic fsync/replace; bounded rotated event log | `test_storage`: private modes, readable atomic JSON, restart persistence, rotation bound and symlink rejection |
| Hold/drain and unsafe admin refusal | real CLI `hold/resume/stop/acknowledge`; `DrainTracker`; `service_cycle` and `supervise` | Native application test: detached harmless live child blocks stop, survives until natural release, then service exits; corrupt controller state and supervision errors hold without exiting coalition; acknowledgement refuses unknown launch identity |
| Installation, rollback, upgrade safety | explicit JSON + LaunchAgent template + README | `plutil -lint` passed; README requires natural drain, source contract re-review on upgrade, safe handoff and supervisor topology proof; no installed changes made |

## Executed gates and receipts

Final test command, executed successfully with the retained Python 3.11 runtime:

```sh
nice -n 10 /Users/jhaynes/.hermes/hermes-agent/venv/bin/python \
  contrib/kanban-resource-controller/run_tests.py \
  --receipt-dir scratch/controller-build/t_206ec88f \
  --label 45-final-acceptance
```

Result: **14 tests passed, 26.004 seconds, exit 0**. Tests are serial in one unittest
process; CLI/process fixtures are sequential, not parallel test suites. The runner
records niceness10/native threads1, clean temporary HOME/HERMES_HOME/KANBAN_HOME,
commands and full real output. No credentials are inherited into tests.

Also executed successfully:

- `nice -n 10 <retained-python> -m compileall -q -j 1 contrib/kanban-resource-controller`
- `plutil -lint contrib/kanban-resource-controller/ai.hermes.kanban-resource-controller.plist.in`
- `git diff --check` (and staged check before commit)
- Static search of new Python: no hardcoded credentials, shell=True, eval/exec,
  pickle, process kill/terminate, private dispatcher imports or forbidden admission
  commands. The one formatted SQL statement is PRAGMA table_info over the fixed
  internal COLUMNS allowlist, not external input.

Ruff was not available either in the retained venv or PATH; no packages were
installed. No full repository suite, npm suite, compileall-j0, remote CI, push or
PR was attempted. Remote CI is not claimed: this is the task's local-only contract.
The repository runner's unconditional compileall-j0 is incompatible with the
explicit task budget, so the handoff-authorized standalone unittest alternative
was used rather than altering test infrastructure.

### TDD receipts retained, including failed attempts

The unique directory `scratch/controller-build/t_206ec88f/` retains real outputs;
failed attempts were not overwritten or relabelled green. Main slice receipts:

- 01→02 resource policy; 03→04 storage/locks; 05→06 command supervision.
- 07→08 argv/process identity; 09→10 canonical schema/config.
- 11→12 engine admission; 13→16 real CLI adapter (fixture and native process-scan
  issues were corrected before the first passing canary).
- 17→18 real snapshot; 19→20 native telemetry/detached child drain.
- 21→22 application lifecycle; 23→26 terminal-card worker handling (canonical
  `_end_run` clears run PID, so spawned events are required).
- 27→28 manual/uncertainty controls; 29→30 acknowledgement.
- 31→32 task/run consistency; 33→34 pre-admission resample.
- 35→36 supervision error preservation; 38→39 exec-changed worker identity.
- 40→41 reader uncertainty reason; 42→43 corrupted state preserves supervision.
- 45-final-acceptance: final aggregate with extra real A→B→A/config-live-change and
  negative-command coverage. Earlier aggregate receipts remain as historical
  evidence, not substitutes for the final run.

One early failing canary uncovered an insufficiently bounded synthetic fixture:
a stub could outlive temporary-directory cleanup. It was released through its own
marker (no production worker was signalled), and the fixture was corrected to
exit on missing root or a finite deadline. Subsequent canaries drain naturally.

## Real canary and no-production-mutation boundary

The canary uses the real supported CLI from this worktree with explicit temporary
HOME/HERMES_HOME/HERMES_KANBAN_HOME. The real dispatcher claims and spawns; its
supported HERMES_BIN override selects a harmless finite worker script. No installed
source monkeypatch and no LLM worker inference is involved.

Observed: first `dispatch --max 1` spawned one; second spawned zero while it was
running. Both CLI dispatches completed while the compatible dispatcher singleton
was held. A supported CLI completion transition did not make the still-living
stub disappear from wrapper capacity accounting. The stub exited naturally after
its release marker. ESTOP in the isolated root was preserved byte-for-byte.
Metadata-only boards-list enumeration preserved the isolated live DB bytes.

Production activity in this phase was limited to read-only native host sensor
sampling and the task's own native Kanban coordination records. No production
admission CLI, global config, profile, ESTOP, service, package or source was changed.
No production database was opened through a mutating CLI to test admission.

## Live host telemetry: observed hold, not simulated recovery

`live-telemetry.json` contains two native samples about 30 seconds apart, obtained
with no dispatch/decompose calls:

- 10 logical cores; native pressure 1 (normal).
- load1 was 3.9765625 then 3.4775390625.
- available bytes were 7612268544 then 7419822080.
- paging-in counters increased from 3060439433216 to 3061212725248;
  paging-out from 22268870656 to 22277079040.
- Gate reasons: `resource:cold`, then `resource:paging-in`.

This demonstrates a real paging hold despite low load. **No healthy live host
transition was observed or claimed.** Healthy dwell/admission transitions are
fixture-driven tests, separately labelled.

## Release gates and classified discoveries

### Prerequisite requiring release-owner disposition: CLI stale-timeout parity

Evidence: retained `hermes_cli/config_defaults.py:1797` defaults stale timeout to
14400; `hermes_cli/kanban_ops.py:85-93` does not pass it; supported dispatch signature
`hermes_cli/kanban_db_dispatch.py:1516` defaults to 0. There is no CLI stale-timeout
flag. This was anticipated by the binding contract and is implemented as an
activation hold, not a silent policy change.

Owner: `default/t_da3d77bf`. Acceptance: either an explicit approved default-profile
policy amendment to the supported behavior, or separately approved capability
work with real parity proof. The builder did not make that consequential choice.

### Out-of-scope follow-up: boards-list writes during nominal observation

Issue-ready title: “Make Kanban boards-list counts genuinely read-only.”
Evidence: `hermes_cli/kanban_boards.py:25-32` opens `connect_closing`, which can run
schema/write setup; `_cmd_boards_list` calls it for every board at lines51-57.
Impact: observational wrappers cannot safely invoke the CLI on production DBs
without potential maintenance writes. This artifact avoids that by running the
supported enumerator on metadata-only private replicas and reading canonical DBs
read-only. Suggested acceptance: read-only list against WAL/schema-current DBs
with proof of no writes/migrations, plus honest handling of incompatible schema.
Owner: release coordinator. Tracking status: **pending**, not deduplicated or
published; no remote writes are authorized. Source card `t_206ec88f` and this
handoff provide the evidence for the authorized orchestrator to search/create or
reuse an issue later. No GitHub URL is fabricated.

### Activation/lifecycle gate (not a passed launchd experiment)

The task forbids builder launchd changes. Therefore the native local process/drain
canary was run without bootstrap/bootout. The README explicitly refuses arbitrary
safe-unload claims and explains Darwin coalition risk, natural drain, and limits
of observed ancestry. The release owner must validate its actual launchd topology
using harmless children before activation and inspect affected work before any
service change. The template has not been installed or supervisor-verified.

## Handoff requirements

Review the frozen SHA and cumulative base diff with the contract, this mapping,
actual receipts and classified discoveries. Do not treat code-review requests as
scope expansion. Fix only accepted in-scope defects in this exact linked worktree;
route unrelated findings to authorized deduplicated issue tracking; obtain explicit
amendments for prerequisites. Preserve the interim unpaused services while those
gates run. No destructive worktree cleanup: the committed artifact and untracked
scratch receipts are intentionally retained for the successor.
