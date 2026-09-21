"""Authority regressions through public task/cohort lifecycle."""
import pytest
from hermes_cli import kanban_db as kb
from tests.hermes_cli.review_readiness_helpers import writer_receipts
from hermes_cli import kanban_review_state as state
from hermes_cli import kanban_review_cohort as cohort
from hermes_cli.kanban_db_connect import connect, write_txn


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    owner = kb.create_task(conn, title='approved ask', assignee='builder')
    attempt = state.enroll_review(conn, owner, expected_status='ready', expected_run_id=None, expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a'*64, base_sha='b'*40, target_sha='c'*40, implementer_maker='openai',
        roster=sorted(state.REQUIRED_LANES), consumed={'rounds':0,'recovery':0,'active_seconds':0},
        compatibility=writer_receipts(conn), decision='synthetic operator')
    state.reserve_action(conn, owner, category='preflight', expected_version=0)
    run = kb.claim_task(conn, owner)
    lanes = {name: {'profile':'reviewer','model':'claude-sonnet-4-5','provider':'anthropic',
                    'workspace':str(tmp_path / name)} for name in attempt['roster']}
    yield conn, owner, attempt, run, lanes
    conn.close()


def test_cohort_requires_runtime_receipt_for_successful_preflight(prepared):
    conn, owner, attempt, run, lanes = prepared
    assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
    with pytest.raises(ValueError, match='implementer runtime receipt'):
        cohort.start_cohort(conn, owner, lanes=lanes, expected_version=1)
    assert not conn.execute('SELECT 1 FROM review_rounds').fetchone()
    assert state.get_attempt(conn, owner)['completed_rounds'] == 0


def test_stale_aggregate_cannot_release_an_operator_hold(prepared):
    conn, owner, attempt, run, lanes = prepared
    cohort.record_runtime_route(conn, owner, run.current_run_id,
                                provider='openai', model='gpt-5', isolated=False)
    assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
    round_id = cohort.start_cohort(conn, owner, lanes=lanes, expected_version=1)
    import json
    member_task = conn.execute('SELECT task_id FROM review_members WHERE round_id=? LIMIT 1', (round_id,)).fetchone()[0]
    contract = json.loads(kb.get_task(conn, member_task).body)
    assert contract['approved_ask']['title'] == 'approved ask'
    assert contract['receipt_contract']['evidence_kinds'] == ['executed', 'reasoned']
    stale = state.get_attempt(conn, owner)
    # Reconciliation may be given a previously-read attempt after a hold. Even
    # complete historical lane evidence cannot confer current dispatch authority.
    with write_txn(conn):
        conn.execute("UPDATE review_members SET state='received_valid',receipt=? WHERE round_id=?",
                     (json.dumps({'verdict':'approve'}), round_id))
        state.hold(conn, stale, 'operator_hold')
        cohort.aggregate(conn, stale, round_id)
    assert state.get_attempt(conn, owner)['state'] == 'held'
    assert state.get_attempt(conn, owner)['completed_rounds'] == 0
    assert kb.claim_task(conn, owner) is None


@pytest.mark.parametrize('mutation', ['severity', 'location', 'execution', 'verification', 'false_execution', 'severity_type'])
def test_lane_completion_rejects_untyped_evidence(prepared, mutation):
    conn, owner, attempt, run, lanes = prepared
    cohort.record_runtime_route(conn, owner, run.current_run_id, provider='openai', model='gpt-5', isolated=False)
    assert kb.request_review(conn, owner, expected_run_id=run.current_run_id)
    round_id = cohort.start_cohort(conn, owner, lanes=lanes, expected_version=1)
    member = conn.execute('SELECT * FROM review_members WHERE round_id=? LIMIT 1', (round_id,)).fetchone()
    task = member['task_id']
    worker = kb.claim_task(conn, task)
    cohort.record_runtime_route(conn, task, worker.current_run_id, provider='anthropic', model='claude-sonnet-4-5', isolated=True)
    evidence = {'kind':'executed', 'command':'test negative count', 'result':'negative count accepted'}
    finding = {'severity':'high', 'location':{'path':'subject.py','line':2},
               'evidence':evidence, 'required_change':'reject negative counts'}
    receipt = {key:attempt[key] for key in ('board_id','base_sha','target_sha','policy_digest','spec_digest')}
    receipt.update(attempt_id=attempt['id'], round_id=round_id, task_id=task, run_id=worker.current_run_id,
                   mandate=member['mandate'], verdict='request_changes', findings=[finding],
                   verification_run=[evidence], prior_findings=[])
    if mutation == 'severity':
        finding['severity'] = 'whatever'
    elif mutation == 'severity_type':
        finding['severity'] = ['high']
    elif mutation == 'location':
        finding.pop('location')
    elif mutation == 'execution':
        finding['evidence'] = 'executed maybe'
    elif mutation == 'verification':
        receipt['verification_run'] = ['I ran something']
    else:
        receipt['verification_run'] = [{'kind':'executed', 'reasoning':'not actually run'}]
    assert kb.complete_task(conn, task, expected_run_id=worker.current_run_id, metadata={'bounded_review':receipt})
    assert conn.execute('SELECT state FROM review_members WHERE task_id=?', (task,)).fetchone()[0] == 'invalid'
    assert state.get_attempt(conn, owner)['completed_rounds'] == 0
