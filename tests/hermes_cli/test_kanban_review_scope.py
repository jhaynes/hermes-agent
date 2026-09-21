"""A scope approval must map the ask, not merely report a small diff."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_cohort as cohort
from tests.hermes_cli.test_kanban_review_authority import prepared


def test_scope_approval_without_requirement_mapping_is_invalid(prepared):
    conn, owner, attempt, run, lanes = prepared
    cohort.record_runtime_route(conn, owner, run.current_run_id, provider='openai', model='gpt-5', isolated=False)
    assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
    round_id = cohort.start_cohort(conn, owner, lanes=lanes, expected_version=1)
    task = conn.execute("SELECT task_id FROM review_members WHERE round_id=? AND mandate='scope'", (round_id,)).fetchone()[0]
    worker = kb.claim_task(conn, task)
    cohort.record_runtime_route(conn, task, worker.current_run_id, provider='anthropic', model='claude-sonnet-4-5', isolated=True)
    receipt = {key: attempt[key] for key in ('board_id', 'base_sha', 'target_sha', 'policy_digest', 'spec_digest')}
    receipt.update(attempt_id=attempt['id'], round_id=round_id, task_id=task, run_id=worker.current_run_id,
                   mandate='scope', verdict='approve', findings=[], prior_findings=[],
                   verification_run=[{'kind': 'reasoned', 'reasoning': 'The diff is small.'}])
    assert kb.complete_task(conn, task, expected_run_id=worker.current_run_id, metadata={'bounded_review': receipt})
    assert conn.execute('SELECT state FROM review_members WHERE task_id=?', (task,)).fetchone()[0] == 'invalid'
