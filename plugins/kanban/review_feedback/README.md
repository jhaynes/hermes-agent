# Staged bounded-review rollout (NOT activated)

These assets are staged installation inputs for an authorized operator. Local implementation and isolated verification are separate from operational rollout. Before installation or enrollment, verify the exact revision's acceptance and independent-review evidence, qualify the installed runtimes, and obtain the applicable operational authorization. This directory is not a deployment receipt and does not activate the workflow.

## Prepared assets

- `rollout.json`: four additional reviewer profiles, attack mandates, finite turn settings and toolset selections. Existing specialist profiles remain mandatory under their applicability rules.
- `skill-patches.json`: exact patches to the four existing default-profile workflow skills. The obsolete fresh-finding round-three repair exception is removed; managed full-cohort replacement takes precedence over old lane-only retry prose.
- `bounded-review-feedback.md`: common procedural reference installed beside each patched skill.
- `verified-procedures.jsonl`: initially empty reference, installed only beside development-lifecycle. Never replace an existing reference with this empty seed.

No credentials or model subscriptions are copied. The profile settings are tested through `create_profile` and `set_config_value` against a temporary home, not against live profiles. Generate inspection copies and before/after hashes when preparing the exact skill patches; those copies are not replacements for unrelated references.

## Supported parent setup, after readiness

1. Verify exact reviewed code SHA and import paths for CLI, gateway and dashboard. Source tests do not prove that the Homebrew gateway or worker launcher has this code. Any runtime upgrade/restart remains a separate operator choice.
2. For each profile in `rollout.json`, use `hermes profile create NAME --no-alias --no-skills --description DESCRIPTION`. Do not clone config, credentials, channels or skill trees. Existing-name conflicts require inspection, not overwrite.
3. For each manifest setting, use `hermes -p NAME config set KEY VALUE`. Preserve current capacity limits. Pin the actual opposite-maker provider/model on every lane card; a profile name is not maker evidence.
4. In the authorized default-profile session, apply each exact patch with `skill_manage(action='patch', ...)`; refuse stale anchors and verify the result. Apply the explicit conflicting-section removal as a targeted patch too. Add the common reference using the supported supporting-file operation. Seed the procedural JSONL only if absent. Do not mutate another named profile's skills.
5. Keep `kanban.review_feedback.auto_apply_lessons` false for the first report-only operational pilot. Select distinct verified reporter/validator profiles with supported config commands only after their capability/runtime checks. Hash the installed protected development-lifecycle SKILL.md and record it through `hermes config set kanban.review_feedback.protected_skill_hash DIGEST` before any separately enabled automatic additions.
6. Exercise the accepted isolated pilot suite, including positive/negative application, rollback, restart, concurrency, complete cohort attacks and actual rendered card/attachment visibility. Toolsets and prompt wording do not enforce a filesystem/network sandbox. Unavailable live attacks stay unverified.
7. Refresh live ownership through supported board reads. Migrate only at a quiescent safe boundary with explicit known history; preserve frozen rounds and consumed budgets. Unknown or exhausted history remains held; do not invent zero counts.

## Rollback hold

Keep the admission guard and additive tables. Quiesce workers, preserve unique/dirty worktrees, park unresolved enrolled tasks and disable new intake before reverting code. A legacy dispatcher lacking the guard must remain stopped against enrolled boards until a supported operator disposition exists. Do not delete state, reset budgets or use reassignment/task recreation as renewal authority.

Use `hermes config set kanban.review_feedback.intake_enabled false` to stop new
operator enrollments; it deliberately does not disable gates on existing attempts.
The effective registered `rounds`, `recovery` and `active_seconds` settings are
copied into each attempt at enrollment. Later configuration edits do not rewrite
that attempt's policy or replenish its counters.

Supported board export preserves the managed tables and parks copied work. Import
creates a new board identity, retains original receipt identities as provenance,
cancels copied launch reservations and leaves managed work held. An imported board
is not an authorized successor. A raw disaster-recovery backup is different: its
original identity must remain with its owning dispatcher, never two live writers.

Procedural-reference rollback remains available after automatic application is
disabled, but still requires the default-profile destination, protected skill hash,
exact current reference hash and recorded operator decision. It does not remove
the incident, validator or before/after audit records.

## Isolated verification

`scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_review_pilot.py` executes
the eight mandatory lane workers through the ordinary dispatcher, source CLI,
local streaming endpoint, task-detail tool, terminal probe and completion tool.
The endpoint is a deterministic test fixture: it is not independent human/model
review, live provider attestation, or billed-model usage evidence. The pilot repairs
a negative-input bug and trims an out-of-scope file at successive snapshots, then
checks both clean third-round completion and third-round rejection/no fourth launch.
The fixture's basic specialist probes do not replace production specialist review.

`test_kanban_review_worker_live.py` separately exercises actual implementer routing,
opposite-maker reviewers, mismatches and revocation before an outer transport retry.
`test_kanban_review_native_transport.py` additionally exercises supported native
Responses and summary-send boundaries, including reconnect admission. Managed
shared-client routes require zero SDK retries; unsupported retry settings fail
closed. These local fixtures do not attest live providers or arbitrary custom
transports. Toolset selection and prompt wording do not supply a filesystem/network
sandbox.

## Operational readiness limits

Use the tracked [writer readiness and legacy-history adoption guide](operator-readiness.md)
and [diagnostic workflow guide](diagnostics.md) for the supported contracts. Release
verification must identify the exact tested and independently reviewed revision;
private builder logs and intermediate run reports are not prerequisites for reading
or using this documentation. Passing isolated tests does not establish installed
runtime compatibility, live provider qualification, profile/skill installation, or
completion of the report-only operational pilot. Those rollout gates remain
separate from local implementation and code review.

## Operator decision receipts (isolated implementation only)

The existing `hermes kanban --board BOARD enroll-review TASK --receipt FILE`
entry point also accepts a scoped decision receipt. This is a trusted operator
boundary, not authentication of arbitrary code running as the account owner.
Worker contexts are refused. Do not use this as live rollout authorization.

Every decision includes `operation`, `attempt_id`, `expected_version`, `board_id`,
`spec_digest`, `base_sha`, `target_sha`, `approved_by` (the recorded Justin
decision), `decision` (its scoped text), and `findings` (every historical finding
ID, including previously adjudicated findings). Read current identities from
`show --json`; a stale version, different owner, live worker or running action
refuses the transaction. Successful decisions never themselves reserve a launch.

- `resume`: restores the recorded held phase without changing counters or policy.
  Exhausted, uncertain-clock and unknown-history attempts cannot use this route.
- `cancel`: parks the owner and cancels unlaunched reservations without refunding
  recovery. Quiesce/reclaim live workers through the supported lifecycle first.
- `amend`: adds `amendment: {ask, spec_digest}`; the digest must be SHA-256 of the
  UTF-8 approved ask. It remains held, preserves original task text and counters,
  and refuses an active/reserved frozen round. The old digest remains lineage
  history, not permission to enroll a renamed copy.
- `successor`: adds `successor: {task_id, base_sha, target_sha, allowance, compatibility}`.
  The named new task must be quiescent and unenrolled. `allowance` explicitly
  specifies finite `rounds`, `recovery`, `active_seconds`; the old attempt stays
  held/cancelled with its counters intact. The successor has a new attempt UUID,
  immutable policy, and durable predecessor link. Original findings remain in
  subsequent review briefs. No predecessor can authorize two successors.
- `reject_finding`: adds `disposition: {finding_id, evidence}` after a settled
  review. Original lane receipts are retained unchanged. A later reviewer may
  cite `rejected` only when this separate parent disposition exists; the
  disposition does not turn the old round into a clean approval.

The three-integer compatibility assertion is now refused. See
[writer readiness and legacy-history adoption](operator-readiness.md) for the
supported challenge/receipt flow and explicit legacy choices. These isolated
implementation paths do not attest installed live services or authorize rollout.
Operational adoption still requires the runtime and history checks described there.

## Typed lane evidence

`verification_run` is a nonempty list of typed evidence objects. Evidence is
either `{kind: executed, command: TEXT, result: TEXT}` or
`{kind: reasoned, reasoning: TEXT}`. A reasoning record never claims a test ran.
Each finding supplies `severity` (`critical`, `high`, `medium`, `low`),
`location: {path: REPO_RELATIVE_PATH, line: POSITIVE_INTEGER}`, typed `evidence`,
and `required_change`. The receiver generates the finding ID and provenance from
the actual attempt/board/run/round/mandate and snapshot; callers do not author
those provenance fields. Prior closures contain `finding_id`, `status`
(`open`, `closed`, or parent-authorized `rejected`), and typed `evidence`.
Duplicate closures, unsupported severities, ambiguous execution claims and
approve-with-actionable-findings invalidate the lane. This validates structure
and recorded authority, not the semantic truth of an arbitrary review narrative.

The subprocess pilots seed synthetic public-catalog metadata inside disposable
profile homes and forbid non-loopback sockets. This prevents CLI cost-guard
catalog startup from silently depending on a public API. CLI/agent/request/tool
execution is real; catalog entries and endpoint responses are labeled fixtures.
Reviewer construction excludes delegation, task creation and cron tools, and
direct delegation/task-create calls are also refused. Local terminal access is
still not an OS security sandbox; isolated probes remain mandatory.
