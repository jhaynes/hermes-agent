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

## Known readiness limits

Read `.hermes/review-feedback-acceptance.json` and `.hermes/review-feedback-result.json` in the builder worktree for exact-SHA evidence and remaining requirements. The staged commands above are not evidence that live setup, sandbox enforcement, independent review, runtime compatibility, publication or deployment occurred.
