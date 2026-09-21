# Writer readiness and legacy-history adoption

This is an operator-only, board-by-board procedure, not authorization to activate
an installed service. Do not restart or upgrade live runtimes merely to make this
preflight pass. Local isolated tests are not a live compatibility receipt.

## Obtain receipts from the writers that will own this board

1. Write a receipt file containing `{"operation":"readiness"}`. Invoke the
   existing `hermes kanban --board BOARD enroll-review TASK --receipt FILE`
   command from the intended CLI installation. It creates a board-local challenge
   and prints that CLI's capability receipt. Save its `challenge_id` and `id`.
2. Ask the owning gateway's existing local control socket for the
   `review-readiness` verb, with params `{challenge_id, board}`. The supported
   Python client is `gateway.control_socket.query_gateway_control(home,
   "review-readiness", params={"challenge_id": CHALLENGE, "board": BOARD})`.
   It returns the receipt directly, or `None` if unavailable/refused. Do not
   substitute a new process claiming to be the installed gateway.
3. POST `{"challenge_id": CHALLENGE}` to the owning dashboard's authenticated
   `/api/plugins/kanban/review-readiness?board=BOARD` route. Preserve its returned
   `id`. Use the existing dashboard authentication; never put credentials in an
   enrollment receipt.
4. Set enrollment `compatibility` to the following selection, using the actual
   returned identifiers, not version numbers:

   ```json
   {
     "challenge_id": "RETURNED_CHALLENGE",
     "cli": "RETURNED_CLI_RECEIPT_ID",
     "gateway": "RETURNED_GATEWAY_RECEIPT_ID",
     "dashboard": "RETURNED_DASHBOARD_RECEIPT_ID"
   }
   ```

Enrollment resolves these IDs in the target board's transaction. Missing,
superseded, foreign-board/path, unknown protocol/Python ABI, differing loaded
workflow code, or no-longer-live serving-writer receipts refuse enrollment before
its state changes. A new challenge invalidates older readiness selections; it
does not change enrolled counters. A serving receipt is bound to host/PID/start
identity. The CLI is a finite process and its loaded capability is checked again
by the enrolling CLI. This is a local capability check, not cryptographic process
attestation or a sandbox against code executing as the same account owner.

The digest covers loaded managed-workflow functions and shared lifecycle entry
points, not merely a checkout SHA, package version string, or file on disk. A
process whose workflow implementation/ABI differs from an enrolled attempt may
not launch its work or mutate its task fields. The persistent writer guard allows
only parking otherwise unchanged work in `blocked`/`needs_input`; reassignment,
workspace/budget changes and clearing that hold are refused. A runtime upgrade therefore requires deliberate forward
compatibility/disposition, not a silent replacement. Unknown older receipt shapes
remain held; there is no automatic compatibility waiver. If a service cannot
answer, leave migration held and present its actual upgrade/restart choice.

A managed successor's `successor` object must now include its own `compatibility`
selection as well as `task_id`, `base_sha`, `target_sha`, and `allowance`. Old
predecessor evidence is preserved but does not authorize new readiness.

## Inspect and decide legacy history

Fresh enrollment receipts must include `expected_assignee` alongside
`expected_status` and `expected_run_id`. Use the assignee from the supported task
read, or JSON `null` only for an actually unassigned task. This is an equality
condition, never a wildcard: a reassignment before enrollment refuses the whole
transaction. Legacy adjudication retains its history-bound receipt format.

Use the same `enroll-review TASK --receipt FILE` command with:

```json
{"operation":"legacy-history", "disposition":"inspect"}
```

The response contains `board_id`, `status`, `expected_run_id`, `spec_digest`,
`history_digest`, proven `lower_bounds`, and redacted prior event/run receipts.
`spec_digest` is SHA-256 of the sorted JSON object `{title, body}` for the current
card. Do not compute it from a different ask. The history digest binds the current
owner/phase plus the exact prior events and runs, not just a count. A new event or
changed owner invalidates the decision; inspect again rather than overriding CAS.

A decision file has exactly these fields:

- `operation`: `legacy-history`.
- `disposition`: `preserve_legacy`, `enroll`, or `successor`.
- `history_digest`, `board_id`, `spec_digest`: from inspection.
- `expected_status`: inspected `status`; `expected_run_id`: inspected value.
- `base_sha`, `target_sha`: full frozen Git SHAs for this approved ask.
- `approved_by`: `Justin`; `decision`: the actual scoped authorization text.
- `consumed`: explicit `{rounds, recovery, active_seconds}` or `null` when unknown.
- `compatibility`: the verified selection above (required for managed adoption).
- `implementer_maker`: known actual maker; `roster`: all applicable mandatory lanes.

Workers cannot authorize these actions. A running owner, unsettled run, or
ongoing legacy review refuses adjudication. Quiesce through the normal lifecycle;
never change the owner just to pass preflight. The original events/runs remain in
the board and a separate redacted history/decision journal is retained atomically.

Choices:

- `preserve_legacy` explicitly keeps the old workflow and does not create a
  managed attempt or retroactive lane approvals. It needs no readiness waiver:
  it is simply not managed adoption. Unknown `consumed` remains null in its receipt.
- `enroll` requires conservative accepted numeric counts, at least the observed
  lower bounds. Unknown is not zero. Existing execution cannot be directly
  enrolled around this decision. A clean legacy verdict starts a new preflight
  within remaining allowance, never approved new lanes. Known exhausted rounds
  or active time create a managed hold, not a free round.
- `successor` additionally supplies `successor: {task_id, base_sha, target_sha,
  allowance, remaining_finding_events}`. The last field must contain every
  `changes_requested` source event ID from inspection, in source order. The new
  target must be distinct, quiescent, unenrolled, and non-diagnostic. `allowance`
  explicitly defines the separately approved finite rounds/recovery/active time.
  If old usage is unknown, the predecessor is conservatively charged to the
  policy ceilings and held; the journal keeps `consumed: null` separately from
  `charged_consumed`, so these are not falsely reported as measured history.
  The successor links to that predecessor. One predecessor cannot mint another.

Changing task IDs, policy, or receipt prose is not a renewal. Retain the old ask,
uncertainty and finding receipts. A failed CAS/adjudication rolls back the journal
and enrollment together. Disabling intake does not release existing holds; an
old SQLite writer without the registered admission function cannot mutate managed
status. Drain and stop an incompatible dispatcher before rollback. Never erase
managed tables or claim that feature-disable makes legacy dispatch safe.

## Isolated verification

Managed SDK sends, including shared-primary Responses streams and iteration-limit
summaries, must use the admitted endpoint/model and recheck the current run,
identity, hold and deadline at each HTTP send. Responses reconnects perform the
same check. Shared clients with SDK retries enabled are refused rather than
silently used outside the finite request-local contract; unsupported transports
remain unavailable for managed work. This does not change unmanaged routing.
Readiness fingerprints include these loaded native-send entry points. Loopback
synthetic endpoint tests do not attest a live provider or an installed runtime.

Run `scripts/run_tests.sh -j 1 tests/hermes_cli/test_kanban_review_readiness*.py
 tests/hermes_cli/test_kanban_review_legacy*.py tests/hermes_cli/test_kanban_review_operator.py`
(as one shell command). The live-readiness test starts disposable gateway-control
and dashboard HTTP servers, invokes separate real CLI processes, reads persisted
receipts, then stops the server and proves stale-process admission is refused.
It does not start, upgrade or attest any installed live service.
