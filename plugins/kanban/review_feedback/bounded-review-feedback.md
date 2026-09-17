# Bounded review feedback: operator-staged contract

This reference describes the approved contract, not a claim that every acceptance test or live rollout has passed. The task's exact-SHA acceptance ledger is authoritative for implementation readiness. Do not activate from an implementation worker.

## Intake and authority

Use supported Kanban reads to identify the owner, current run, board identity and consumed history. Stop enrollment if runtime compatibility or history is uncertain. Preserve unmanaged legacy work separately. The operator receipt names the board, current phase/run, spec digest, base and target SHA, maker, mandatory roster, consumed counters and the recorded decision. `enroll-review` is operator-only; the builder cannot mint fresh lineage authority.

Through `terminal`, the finite lifecycle is:

- `hermes kanban enroll-review <task-id> --receipt <receipt.json>`
- `hermes kanban reserve-review-action <task-id> --category preflight --expected-version <version>`
- After the preflight worker's supported review handoff: `hermes kanban start-review-cohort <task-id> --lanes <lanes.json> --expected-version <version>`
- Use `--recovery` only for a failed allowed preflight/repair or invalid whole cohort. The durable reservation is consumed before launch.
- Inspect `hermes kanban show <task-id> --json`; worker briefs carry the same attempt identity.

These commands do not grant publication, merge, credential changes, service replacement or active-task migration. Unsupported successor/resumption cases remain held for an explicit operator decision; never emulate them by editing SQL or recreating cards.

## Required cohort

Retain tests, quality, architecture and style; add docs/system when their existing applicability rules require them. Add:

1. Breaker A: malformed/boundary inputs and error-path assumptions.
2. Breaker B: concurrency, timing, cancellation and resource pressure; explain real non-applicability.
3. Breaker C: lifecycle transitions, retries, rollback and integration regressions.
4. Scope: blocking mapping of the entire diff to the original ask/amendments and acceptance evidence.

Each lane returns exact board/attempt/task/run/round, policy/spec digest, mandate, base/target SHA, verdict, findings, actual verification and prior-finding closure. Label unexecuted reasoning. “No supported remaining findings” is not proof of absence of bugs. Do not reward manufactured findings.

Maker comes from actual worker routing, not a profile name or reviewer declaration. Unknown or same-maker routes and unverified fallback are invalid. The source snapshot stays immutable. Run mutation probes only in isolated disposable artifacts; toolset selection and prompt language do not constitute an OS sandbox. Live filesystem/network attacks remain unverified until restrictions are demonstrated.

## Budgets and holds

Three completed valid whole-cohort rounds; two recovery action reservations across the lineage; 120 union-active minutes. A clean third round may approve. A third rejection cannot dispatch another repair, test-only cap-tail correction or substantive review. Scope edits consume the next full round too. Whole-cohort replacement retains old evidence as history, not reusable approval.

Claim/release transitions own timing. Explicit human/dependency/capacity waits with no live execution do not run the clock. A real timeout remains failure. Unknown restart/clock state requires conservative reconciliation or hold. Never reset a counter by reassignment, replacement, policy version, amended spec or rollback.

Preserve held, dirty and uniquely committed worktrees. Stop intake before rollback; retain the admission guard and state. A legacy runtime must not restart against unresolved enrolled boards without a supported disposition. No state-table deletion or blanket cleanup.

## Incidents, reports and learning

Every actual block and execution failure is durable evidence. Expected typed waits produce deterministic records; unknown failures do not become harmless because their prose contains “approval” or “timeout”. Capture/report/lesson/recovery status are separate. Reports use the existing queue, with one synthesis plus one infrastructure retry and ten active minutes. A reporter or validator cannot recursively spawn incident work or unblock implementation.

The initial learning mode is report-only. Automatic application, when separately enabled after acceptance, is restricted to exact schema-valid records in `verified-procedures.jsonl`. Initial procedure `record-worker-deadline` only clarifies retaining evidence already required by the deadline procedure: source timeout event, elapsed and limit fields, and the existing `hermes kanban runs <task-id> --json` inspection. The independent validator replays the numeric comparison against the preserved event; it does not execute log-proposed commands or infer root cause.

Any extra tools, permissions, destinations, numeric limits, reviewer changes, product scope, completion rule, free-form Markdown policy edit or uncertain diagnosis requires operator approval. A data record has no precedence over policy or the ask. Preserve validator provenance, hashes, journaled before/after images and rollback evidence. Later briefs cite the lesson version; never mutate a cached system prompt.

## Verification and deployment ownership

The parent owns independent frozen-SHA review, live profile/skill installation, runtime compatibility across CLI/gateway/dashboard, subscriptions and rollout. First exercise positive, negative, restart, concurrent-update and rollback cases in temporary homes/boards. Report every unverified row explicitly. A successful unit test, model endpoint response, staged profile, HTML 200 or applied reference file alone does not establish full workflow completion or deployment.
