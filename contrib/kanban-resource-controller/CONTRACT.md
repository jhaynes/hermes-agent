# Implementation scope contract

## Authority and provenance

Justin's explicit request, as preserved by the release owner:

> Ok, let's get the resource aware controller built. unpause things please.

Live tracker task `t_206ec88f` (builder3), original body:

> Justin explicitly authorized implementation and unpause. Read FULL binding handoff /Users/jhaynes/.hermes/plans/hermes-resource-controller-build-handoff.md and reviewed design /Users/jhaynes/.hermes/plans/hermes-kanban-build-only-controller.md before editing. New handoff supersedes stale migration/pause facts. Finite implementation only: complete standalone external helper, strict serial TDD, real isolated CLI canary, operating docs/install template, committed exact snapshot, acceptance receipts. Stage only contrib/kanban-resource-controller/ in this linked worktree; no existing Hermes source edits or installed-source/profile/config/service changes, no remote writes. No delegates or parallel test suites. Existing runtime base5910de20 is intentional for runtime compatibility, not upstream tip. Keep resource budget one test process/native threads1/niceness10; no full suite or compileall-j0. Return finite implementation with native kanban_complete and exact SHA/worktree to release owner default/t_da3d77bf, who owns independent review and activation; do not label feature installed. First milestone must acknowledge scope, runtime/worktree identity and successor. Handoff includes all thresholds, fail-closed behavior, CLI concurrency semantics, process identity, decomposition, lifecycle, timeout/ESTOP/lock/compatibility tests and proof requirements. Do not revive old adaptive-core branch or create another plan-only task.

Both external source documents were read in full before implementation. Their
SHA256 values were measured on the implementation host:

| Binding source | SHA256 |
|---|---|
| `/Users/jhaynes/.hermes/plans/hermes-resource-controller-build-handoff.md` | `a897e4fb59e929754026e106f7af2316f389161ee830d1bb2b48fa10a5bec65d` |
| `/Users/jhaynes/.hermes/plans/hermes-kanban-build-only-controller.md` | `1d76749432f8cbf69be78bdac99ae3470e6349f36ca0037da58cac3f80a62213` |

The newer handoff supersedes stale operational facts, not reviewed policy. No
new product/architecture amendment has been inferred. Runtime consolidation is
already done; the interim unpaused embedded max5 policy remains unchanged during
this build. Existing excess workers finish, never get killed to reach cap2.

Original base: `5910de20bc9839fdd36e791a9d72ba2c2e722f66`.
Worktree: `/Users/jhaynes/.hermes/hermes-agent/.worktrees/t_206ec88f`.
Branch: `ops/kanban-resource-controller`.

## Pre-edit acceptance mapping

The first durable task comment recorded this mapping before edits:

| Change | Acceptance |
|---|---|
| Resource policy and native telemetry | thresholds, both paging directions, cold/reset/unknown/gap, 120s dwell, cooldown |
| Canonical read-only inventory and compatibility adapter | supported CLI/schema/config parity, exact worker PID+creation identity, host2/profile1 including terminal cards, unowned notification gate |
| Command supervision and decision loop | shared lock, ESTOP preservation, one command/window, board rotation, decomposition toggle/fairness, pre/post reconciliation and persistent uncertainty |
| Private storage and entrypoint | atomic status, bounded logs/output, observation without authority, manual hold/drain, restrictive artifacts |
| Real isolated supported-CLI canary and native synthetic children | concurrency semantics, lock compatibility, no production queue mutations, safe admission-only lifecycle |
| README/template/IMPLEMENTATION and scratch receipts | deployment/rollback constraints, exact-snapshot finite handoff |

Every implemented file is classified `in_scope_required`. Test-discovered defects
in this new implementation were corrected in the same scope. Existing Hermes core
behavior is not modified. Discoveries outside that boundary are recorded in
IMPLEMENTATION for release-owner disposition, not bonus implementation.

## Non-goals and permission boundaries

No existing source edits, installed runtime changes, named-profile changes,
Smithers work, package/dependency changes, production queue admission tests,
production DB repair, fabricated notification/process ownership, remote writes,
worker interruption, live launchd changes, broad ESTOP manipulation, delegate
fleet, full repository suite, or old adaptive-core branch activation.

The task graph has a pre-created release child, `default/t_da3d77bf`. Native
completion of this card releases that finite successor; it is not feature approval
or installation. The release owner must include opposite-maker specialist review
and the dedicated `reviewscope` reviewer in every round, with exact SHA, these
contract sources, original base, cumulative diff, change-to-acceptance mapping,
classified discoveries, actual verification and a clear approve/request_changes
verdict. An unresolved scope veto blocks delivery. Reviewer demands do not expand
scope. New prerequisites require explicit amendment; unrelated findings require
deduplicated issue tracking by the authorized owner. No remote publication is
currently authorized, so pending issue-ready findings are not labelled tracked.
