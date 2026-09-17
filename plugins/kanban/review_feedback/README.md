# Staged bounded-review rollout (NOT activated)

These assets are local installation inputs for the authorized parent. They are not a deployment receipt. Do not install, enroll live cards, replace services or commission final independent review until the implementation acceptance ledger has no unresolved implementation requirements.

## Prepared assets

- `rollout.json`: four additional reviewer profiles, attack mandates, finite turn settings and toolset selections. Existing specialist profiles remain mandatory under their applicability rules.
- `skill-patches.json`: exact patches to the four existing default-profile workflow skills. The obsolete fresh-finding round-three repair exception is removed; managed full-cohort replacement takes precedence over old lane-only retry prose.
- `bounded-review-feedback.md`: common procedural reference installed beside each patched skill.
- `verified-procedures.jsonl`: initially empty reference, installed only beside development-lifecycle. Never replace an existing reference with this empty seed.

No credentials or model subscriptions are copied. The profile settings are tested through `create_profile` and `set_config_value` against a temporary home, not against live profiles. The staged full skill texts and before/after hashes are generated separately under the builder worktree's `.hermes/staged-review-skills/`; these are inspection copies, not replacements for unrelated references.

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
These checks do not yet establish enforcement at every possible SDK retry,
reconnect, custom transport or client replacement boundary. Do not claim that
toolset selection or prompt wording supplies a filesystem/network sandbox.

## Known readiness limits

Read `.hermes/review-feedback-acceptance.json` and `.hermes/review-feedback-run2-result.json` in the builder worktree for exact-SHA evidence and remaining requirements; the original result files preserve run 1. The staged commands above are not evidence that live setup, sandbox enforcement, independent review, runtime compatibility, publication or deployment occurred.
