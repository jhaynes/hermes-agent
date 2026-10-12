"""U1 configurable termination reserve: seam-contract/v2 capability C1.

Contract basis (docs/contracts/seam-contract-v2.json C1, plan v3 U1):
- config source kanban.review_feedback, default 60, beside rounds/recovery/active_seconds
- stage (a) validation at config load: finite positive integer, smaller than every
  issuable lease/cap duration -> typed invalid_reserve_config
- stage (b) assertion at each lease issuance: remaining > reserve, else typed
  insufficient_safe_window (no lease, lineage parked)
- launcher boundary: never starts a command whose timeout plus reserve reaches the
  action-lease or union-cap boundary -> typed reserve_exceeds_boundary
- per-action reserve recorded in the lease row in the same claim transaction; config
  changes apply only to actions reserved after the change, never retroactively
"""
import importlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.review_readiness_helpers import writer_receipts

ROSTER = ['tests', 'quality', 'architecture', 'style',
          'breaker_a', 'breaker_b', 'breaker_c', 'scope']


@pytest.fixture()
def reserve_mod():
    importlib.invalidate_caches()
    from hermes_cli import kanban_review_state as state
    importlib.reload(state)
    return state


def _enroll(conn, task, state):
    """Enroll using the effective registered config (set config before calling)."""
    return state.enroll_review(
        conn, task, expected_status='ready', expected_run_id=None,
        expected_assignee='builder',
        board_id=conn.execute('SELECT board_id FROM workflow_board').fetchone()[0],
        spec_digest='a' * 64, base_sha='b' * 40, target_sha='c' * 40,
        implementer_maker='openai', roster=ROSTER,
        consumed={'rounds': 0, 'recovery': 0, 'active_seconds': 0},
        compatibility=writer_receipts(conn), decision='synthetic U1 enrollment')


def test_default_reserve_is_60_and_recorded_per_action(tmp_path, monkeypatch, reserve_mod):
    """U1-default-60 + U1-reserve-recorded-per-action: absent config, effective reserve
    is 60 and the lease row records it in the same claim transaction."""
    from hermes_cli.config import save_config
    save_config({'kanban': {'review_feedback': {
        'rounds': 3, 'recovery': 1, 'active_seconds': 7200}}})
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='default reserve', assignee='builder')
        attempt = _enroll(conn, task, reserve_mod)
        assert reserve_mod.effective_reserve() == 60
        reserve_mod.reserve_action(conn, task, category='preflight', expected_version=attempt['version'])
        run = kb.claim_task(conn, task)
        assert run is not None
        row = conn.execute(
            "SELECT termination_reserve_seconds FROM review_actions WHERE task_id=? AND state='running'",
            (task,)).fetchone()
        assert row is not None and row[0] == 60, 'lease row must record the effective reserve'
        # readback: the supported export surfaces the per-action reserve
        from hermes_cli.kanban_review_output import workflow_details
        actions = workflow_details(conn, task)['actions']
        assert actions and actions[0]['termination_reserve_seconds'] == 60
    finally:
        conn.close()


def test_config_validation_typed_refusals(tmp_path, monkeypatch, reserve_mod):
    """U1-config-validation: non-positive, non-integer, non-finite, or lease-exceeding
    termination_reserve_seconds at config load raises typed invalid_reserve_config."""
    from hermes_cli.config import save_config
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    for bad in (0, -5, 7.5, float('inf'), float('nan'), 7201):
        save_config({'kanban': {'review_feedback': {
            'rounds': 3, 'recovery': 1, 'active_seconds': 7200,
            'termination_reserve_seconds': bad}}})
        with pytest.raises(ValueError) as excinfo:
            reserve_mod.effective_reserve()
        assert excinfo.value.args[0] == 'invalid_reserve_config'
    save_config({'kanban': {'review_feedback': {
        'rounds': 3, 'recovery': 1, 'active_seconds': 7200,
        'termination_reserve_seconds': 120}}})
    assert reserve_mod.effective_reserve() == 120


def test_enrollment_refuses_reserve_at_or_above_lease(tmp_path, monkeypatch, reserve_mod):
    """Stage (a) cross-field check: the reserve must be smaller than every issuable
    lease/cap duration at enrollment, where the policy is pinned."""
    from hermes_cli.config import save_config
    save_config({'kanban': {'review_feedback': {
        'rounds': 3, 'recovery': 1, 'active_seconds': 100,
        'termination_reserve_seconds': 100}}})
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='reserve equals lease', assignee='builder')
        with pytest.raises(ValueError, match='invalid_reserve_config'):
            _enroll(conn, task, reserve_mod)
    finally:
        conn.close()


def test_insufficient_safe_window_parks_lineage_without_lease(tmp_path, monkeypatch, reserve_mod):
    """U1-insufficient-window: remaining <= reserve at issuance produces typed
    insufficient_safe_window — no lease, lineage parked, never rounded into extra time."""
    from hermes_cli.config import save_config
    save_config({'kanban': {'review_feedback': {
        'rounds': 3, 'recovery': 1, 'active_seconds': 7200,
        'termination_reserve_seconds': 300}}})
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='no safe window', assignee='builder')
        attempt = _enroll(conn, task, reserve_mod)
        from hermes_cli.kanban_db_dispatch import _record_task_failure
        # burn active time to 6900 of 7200 inside one action, then fail it.
        # remaining 300 == reserve 300: stage (b) holds (remaining > reserve fails).
        reserve_mod.reserve_action(conn, task, category='preflight', expected_version=attempt['version'])
        now = [1000.0]
        monkeypatch.setattr(reserve_mod.time, 'monotonic', lambda: now[0])
        run = kb.claim_task(conn, task)
        assert run is not None
        now[0] += 6900
        _record_task_failure(conn, task, 'failure', outcome='spawn_failed', failure_limit=100,
                             release_claim=True, end_run=True)
        attempt = reserve_mod.get_attempt(conn, task)
        assert attempt['active_seconds'] == pytest.approx(6900)
        assert attempt['state'] == 'preflight'
        action = reserve_mod.reserve_action(conn, task, category='preflight',
                                            expected_version=attempt['version'], recovery=True)
        assert action is not None, 'recovery action is issuable; the gate lives at lease issuance'
        # remaining 300 <= reserve 300: typed refusal, no lease, lineage parked.
        # The claim is revoked inside the same transaction, so the returned task
        # snapshot (if any) already shows the blocked terminal state.
        run = kb.claim_task(conn, task)
        assert run is None or run.status == 'blocked', (
            'remaining == reserve must refuse the lease and revoke admission')
        assert kb.claim_task(conn, task) is None
        attempt = reserve_mod.get_attempt(conn, task)
        assert attempt['state'] == 'held', 'insufficient safe window must park the lineage'
        assert attempt['hold_reason'] == 'insufficient_safe_window'
        assert conn.execute(
            "SELECT COUNT(*) FROM review_actions WHERE attempt_id=? AND state='running'",
            (attempt['id'],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_reserve_recorded_value_survives_config_change(tmp_path, monkeypatch, reserve_mod):
    """U1-reserve-recorded-per-action: config changes apply only to actions reserved
    after the change; earlier receipts keep the value they actually used."""
    from hermes_cli.config import save_config
    save_config({'kanban': {'review_feedback': {
        'rounds': 3, 'recovery': 1, 'active_seconds': 7200,
        'termination_reserve_seconds': 60}}})
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='reserve lineage', assignee='builder')
        attempt = _enroll(conn, task, reserve_mod)
        reserve_mod.reserve_action(conn, task, category='preflight', expected_version=attempt['version'])
        kb.claim_task(conn, task)
        from hermes_cli.kanban_db_dispatch import _record_task_failure
        _record_task_failure(conn, task, 'failure', outcome='spawn_failed', failure_limit=100,
                             release_claim=True, end_run=True)
        save_config({'kanban': {'review_feedback': {
            'rounds': 3, 'recovery': 1, 'active_seconds': 7200,
            'termination_reserve_seconds': 150}}})
        attempt = reserve_mod.get_attempt(conn, task)
        reserve_mod.reserve_action(conn, task, category='preflight',
                                   expected_version=attempt['version'], recovery=True)
        kb.claim_task(conn, task)
        rows = conn.execute(
            'SELECT termination_reserve_seconds FROM review_actions ORDER BY rowid').fetchall()
        assert [r[0] for r in rows] == [60, 150], (
            'earlier receipt keeps the old reserve; the later action uses the new value')
    finally:
        conn.close()


def test_launcher_boundary_subtraction(tmp_path, monkeypatch, reserve_mod):
    """U1-boundary-subtraction: the launcher never starts a command whose timeout plus
    reserve reaches either boundary; the refusal attempt is typed."""
    from hermes_cli.config import save_config
    save_config({'kanban': {'review_feedback': {
        'rounds': 3, 'recovery': 1, 'active_seconds': 7200,
        'termination_reserve_seconds': 120}}})
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    conn = connect(tmp_path / 'board.db')
    try:
        task = kb.create_task(conn, title='boundary', assignee='builder')
        attempt = _enroll(conn, task, reserve_mod)
        reserve_mod.reserve_action(conn, task, category='preflight', expected_version=attempt['version'])
        run = kb.claim_task(conn, task)
        assert run is not None
        action = conn.execute(
            "SELECT id,deadline,termination_reserve_seconds FROM review_actions "
            "WHERE task_id=? AND state='running'", (task,)).fetchone()
        lease_remaining = action['deadline'] - reserve_mod.time.time()
        reserve = action['termination_reserve_seconds']
        allowed_timeout = reserve_mod.launcher_command_timeout(lease_remaining, union_remaining=None)
        assert allowed_timeout is not None and allowed_timeout <= lease_remaining - reserve, (
            'command timeout must leave the full reserve inside the action-lease boundary')
        # a requested window that would reach the boundary is refused, typed
        with pytest.raises(ValueError) as excinfo:
            reserve_mod.launcher_command_timeout(lease_remaining, union_remaining=None,
                                                 requested_timeout=lease_remaining)
        assert excinfo.value.args[0] == 'reserve_exceeds_boundary'
        # union-cap boundary: when the remaining union allowance is tighter, it governs
        union_remaining = lease_remaining - reserve - 5
        allowed = reserve_mod.launcher_command_timeout(lease_remaining, union_remaining=union_remaining)
        assert allowed is not None and allowed <= union_remaining - reserve
    finally:
        conn.close()
