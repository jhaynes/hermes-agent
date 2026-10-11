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
`evidence`, bounded `receipts`, an `evidence_report_contract` and (for validators) a numeric `replay`. A reporter completes with
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
  categorical schema remains readable for existing reports. Schema 2 below supports
  bounded narrative analysis without granting causal or execution authority.
- Recovery recommendations are operator_investigation or operator_decision_required,
  never executable instructions. Validation needed is deterministic_replay,
  independent_reproduction or operator_decision.
- The existing exact procedural-evidence record may enter independent validation.
  Protected proposals use only `{kind, approval_required:true}`; kind is
  policy_change, code_change, limit_change, permission_change, scope_change or
  reviewer_change. They remain pending approval. Hypothesis-only reports cannot
  launch an automatically applicable lesson.

Schema 2 adds `schema: 2` to the same fields and accepts event IDs plus `run:N` and
`review:TASK` citations from `receipts`. Existing run `verification_run` records
and the review cohort reserved before the incident supply test/reviewer evidence;
later rounds cannot replace the old snapshot. Facts must exactly copy a cited
observation. A reviewer reporting a finding is an observed receipt, not proof
that the finding or its proposed cause is true. High observation confidence uses
`cited_receipt_observation_only`. Causal certainty is never inferred automatically.

Hypotheses, contributing conditions and missed gates are bounded objects with
`claim`, `citations` and `status: unverified`. Recovery is an object with `action`,
`citations` and `execution: operator_only`. Protected proposals may include a bounded
`proposal`, but require `approval_required: true`. Hypothesis-only reports are valid;
they cannot authorize automatic learning. Narrative fields are limited to 2048
characters and checked at completion and publication. Evidence is limited to 32
receipts / 16 KiB total, eight verification entries per receipt, and 8 KiB per
receipt. Oversized sources and unavailable redaction omit evidence rather than
forwarding raw data. Secret-bearing text fields are fully replaced, not partially
masked. Raw comments and attachments are never fed to the diagnostic model.

The validator independently receives and checks the recorded timeout comparison;
the completion boundary repeats that deterministic comparison and verifies distinct
run/profile provenance. It never replays commands from a log. A failed validator
cannot acquire another reservation through native/manual unblock.

Application compares the approved procedure fields independently of the source
event. A separately validated equivalent observation retains its lesson and
validator receipt, but is marked rejected with a `lesson_equivalent` event linking
the already-present lesson; it does not append duplicate procedure data. This
comparison runs under the procedural-reference lock, including history from other
boards using that same reference. Contradictory or policy-bearing records do not
match the literal allowlist and cannot replace approved procedure text.

Recurrence moves an applied lesson back to pending approval and removes it from
new advisory briefs. The append-only reference and application journal remain for
audit; retained bytes are not evidence that the procedure prevented recurrence.

## Time, retries and incidents

A report has one synthesis claim plus at most one infrastructure-only retry and
600 cumulative active seconds. Spawn failure, an observed process signal, a quota
exit or a supervisor-proven deadline can qualify; unknown exits, missing completion,
iteration exhaustion and rejected content cannot mint another claim. Historical
retry rows without preserved failure provenance fail closed. A rejected report can
be corrected inside the same finite run, not through an additional synthesis run.
Claim/release and model-request checks use persisted board state. The supervisor
charges surviving processes even after their run closes; ordinary terminal grace
cannot extend the diagnostic deadline. An uncertain boot/clock is conservatively
exhausted rather than reset. Real CLI crash and timeout pilots prove no owner
release, extra synthesis or recursive diagnostic incident.

The task/run reservation exists before process launch. A child whose dispatcher
dies before writing the PID can adopt that still-current reservation with its own
process fingerprint before requesting the model. A late child whose reservation
was released is refused. The interruption pilot uses a real source-CLI child and
reopened board connection; it does not claim an OS-wide crash/reboot simulation.
Diagnostic promotion honors the same admission guard, so a terminal synthesis hold
cannot turn back into Ready through ordinary dependency recomputation.

The same launch adoption is used by managed implementation preflight/repair and
review workers. On POSIX, the dispatcher retains its Popen handles until exit
classification so an unrelated subprocess launch cannot consume a diagnostic's
signal receipt through Python's garbage-collection reaper. A lost dispatcher still
cannot infer an unobserved signal: unknown failure provenance remains fail-closed.

Managed SDK requests pin the resolved provider, model, API mode and endpoint at
admission. Request-local OpenAI/Anthropic clients retain zero SDK retries and a
send hook rechecks model, endpoint, run authority and deadline, including cached
clients and redirects. Unsupported adapters are refused rather than treated as
verified routing. The isolated OpenAI-wire pilot exercises a client-route swap
after preparation and verifies zero requests to the forbidden endpoint; this is
not evidence for every native-provider transport.

Writable imports retain diagnostic counters and receipts but park diagnostic work;
their new board identity is not launch authority. Unknown workflow versions are
refused at diagnostic admission, request and result/publication boundaries. Diagnostic
tasks cannot be reclassified through legacy-history adjudication.

Only a causal terminal cascade is merged. Independent same-run review causes
remain separate. Up to 32 source IDs enter the bounded incident context; all source
links remain in the board audit table. Expected waits contain reason, owner task,
next action and source event. Unassigned ready work or an unavailable assigned
profile can generate a distinct
stalled_queue candidate after the dispatcher's existing nonzero stale threshold;
capacity skips are not execution failures. Comment/attachment activity neither
restarts the stale clock nor creates another episode; a new lifecycle episode does.

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
suppresses a notice not yet admitted by the subscription cursor transaction for
that episode, including a report already published before acknowledgment. It does
not suppress future recurrence. A network send already admitted before the
acknowledgment cannot be recalled; failed-delivery replay rechecks acknowledgment.

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
