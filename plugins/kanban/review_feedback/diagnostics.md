# Bounded diagnostics and postmortems

This is staged implementation, not live activation approval. Keep diagnostics in
isolated test homes until the remaining acceptance ledger and independent review
are satisfied. In particular, finite service under a sustained higher-priority
builder backlog is **not guaranteed by the current scheduler**. The measured
fixture `test_kanban_diagnostic_queue.py` leaves the reporter queued across three
successive builder dispatches. Rollout requires the already-prescribed scoped
capacity/scheduling decision; do not raise global concurrency or invent a second
scheduler to hide this hold.

## Configuration and scope

After separately authorized setup, use the supported configuration interface in
the dispatcher-owning profile:

- `hermes config set kanban.review_feedback.postmortem_profile PROFILE`
- `hermes config set kanban.review_feedback.validator_profile OTHER_PROFILE`
- Leave `kanban.review_feedback.auto_apply_lessons` false for the first pilot.

The validator profile must differ from the report author's actual run profile.
The dispatcher reserves this route when it queues the report. A reporter's own
profile configuration is not authority to change the validator. Existing profile
and board caps remain authoritative. No new profiles or live configuration are
installed by these instructions.

The launcher/agent constructs reporters and validators with only `kanban_show`,
`kanban_complete`, and `kanban_heartbeat`. Show returns their own bounded evidence,
not raw owner context, comments, attachment paths, or peer tasks. The tool handlers
also reject other lifecycle mutations, board overrides, files and arbitrary
metadata. Comment steering is disabled for diagnostics. Malicious model calls to
unavailable file tools are refused by the actual CLI execution path.

This is tool confinement, **not an OS sandbox** against trusted installed plugins,
provider code or arbitrary code already executing as the account owner. Production
filesystem/network attacks remain unavailable/unverified. Tests use disposable
homes, source-pinned CLI workers and a fail-closed loopback-only network bootstrap.

## Evidence and report contract

`kanban_show` supplies `result_contract`, `section_enums`, `fact_contract`, bounded
`evidence` and (for validators) a numeric `replay`. A reporter completes with
`metadata.postmortem`; a validator uses `metadata.lesson_validation`.

Report fields are exact: incident_id, owner, citations, facts, hypotheses,
confidence, confidence_basis, contributing_conditions, missed_gates,
recovery_recommendation, proposed_change and validation_needed. Limits are 32 KiB,
32 unique citations/facts and eight entries per categorical section.

- Facts must exactly match a preserved event's `{id, kind, created_at}`. A citation
  alone cannot turn arbitrary prose, personal blame or an inferred cause into fact.
- `confidence=high` requires facts and
  `confidence_basis=cited_event_observation_only`; this is confidence in a recorded
  observation, not a root-cause attestation. Unknown cause uses `unknown` and
  `cause_not_established`.
- Hypotheses are explicitly unverified categories: cause_unestablished,
  infrastructure_failure, deadline_exhaustion, admission_failure. Contributing
  conditions and missed gates use the enumerations returned by show. This initial
  conservative schema does not accept arbitrary diagnostic prose or certify broad
  causal analyses; wider evidence-backed synthesis remains an acceptance limitation.
- Recovery recommendations are operator_investigation or operator_decision_required,
  never executable instructions. Validation needed is deterministic_replay,
  independent_reproduction or operator_decision.
- The existing exact procedural-evidence record may enter independent validation.
  Protected proposals use only `{kind, approval_required:true}`; kind is
  policy_change, code_change, limit_change, permission_change, scope_change or
  reviewer_change. They remain pending approval. Hypothesis-only reports cannot
  launch an automatically applicable lesson.

The validator independently receives and checks the recorded timeout comparison;
the completion boundary repeats that deterministic comparison and verifies distinct
run/profile provenance. It never replays commands from a log. A failed validator
cannot acquire another reservation through native/manual unblock.

## Time, retries and incidents

A report has at most two synthesis claims and 600 cumulative active seconds.
Claim/release and model-request checks use persisted board state. The supervisor
charges surviving processes even after their run closes; ordinary terminal grace
cannot extend the diagnostic deadline. An uncertain boot/clock is conservatively
exhausted rather than reset. Real CLI crash and timeout pilots prove no owner
release, extra synthesis or recursive diagnostic incident.

Only a causal terminal cascade is merged. Independent same-run review causes
remain separate. Up to 32 source IDs enter the bounded incident context; all source
links remain in the board audit table. Expected waits contain reason, owner task,
next action and source event. Unassigned ready work can generate a distinct
stalled_queue candidate after the dispatcher's existing nonzero stale threshold;
capacity skips are not execution failures.

Inspect existing `show TASK --json`, `runs TASK --json`, comments and attachments.
Incident state, report status, lesson status and publication status are separate.
The owner details include reporter task, runs started, active seconds, deadline,
attachment ID/digest and typed publication errors. A written report never resolves
or resumes implementation.

## Acknowledge or record recovery

Use the existing operator-only receipt boundary:

`hermes kanban --board BOARD enroll-review OWNER_TASK --receipt FILE`

FILE is an object containing exactly:

- operation: incident
- incident_id, board_id: the current board's identifiers
- expected_state: current open/acknowledged/recovered state
- state: acknowledged or recovered
- clearance_events: a list of preserved event IDs (at most 16)

Acknowledgment may have an empty clearance list. Recovery requires a later owner
`completed` or explicit `unblocked` event. Wrong board/task/state or worker-context
calls are refused. The receipt is retained; this operation never unblocks the owner.
Later recurrence gets a new open incident linked to prior history. Acknowledgment
suppresses a not-yet-published notice for that episode, not future recurrence.

## Publication, failures and notifications

Reports are content-addressed JSON attachments on the owner. Publication verifies
exact bytes, adopts a matching orphan blob after a crash, and refuses mismatches or
symlinks. Repeated ticks do not create duplicate attachment rows or blobs. A typed
publication failure does not erase the report or prevent worker supervision.

The `postmortem_report` event uses existing authorized notification subscriptions.
It sends a passive notice through the owning adapter and does not wake implementation
or claim resolution. No destination is created or inferred. A recording-adapter
fixture verifies routing and cursor deduplication; it is not a real external delivery
receipt. A terminal/TUI is not promised asynchronous delivery.

## Verification and remaining limits

Run `scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_diagnostic*.py
 tests/hermes_cli/test_kanban_postmortem.py tests/hermes_cli/test_kanban_workflow_lessons.py
 tests/gateway/test_kanban_postmortem_notifier.py` in a resource-pinned test environment.
The source CLI pilots use explicit synthetic local endpoints, not billed providers
or independent reviewers. Their JUnit properties record request count/bytes,
dispatch-to-terminal time and temporary-tree bytes.

Whole-feature acceptance still includes broader learning equivalence/path races,
rendered dashboard verification, operator/recreation and full transport/launch
recovery contracts, plus independent review and separately authorized rollout.
Do not equate passing these diagnostic tests with completing those gates.
