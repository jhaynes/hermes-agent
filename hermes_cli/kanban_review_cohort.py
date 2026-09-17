"""Whole-cohort reservations and exact-snapshot lane receipts."""
from __future__ import annotations

import json
import hashlib
import uuid
from pathlib import Path

from hermes_cli.kanban_db_connect import write_txn


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS review_rounds (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        recovery INTEGER NOT NULL, state TEXT NOT NULL, base_sha TEXT NOT NULL,
        target_sha TEXT NOT NULL, UNIQUE(attempt_id,ordinal,recovery))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS review_members (
        task_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, round_id TEXT NOT NULL,
        mandate TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
        run_id INTEGER, provider TEXT, model TEXT, maker TEXT, isolated INTEGER,
        UNIQUE(round_id,mandate))''')


def start_cohort(conn, task_id, *, lanes, expected_version, recovery=False):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_review_state as state
    with write_txn(conn):
        attempt = state.get_attempt(conn, task_id)
        if not attempt or attempt['version'] != expected_version:
            raise ValueError('attempt CAS lost')
        if task_id!=attempt['task_id']:
            raise ValueError('only the implementation owner can reserve a cohort')
        if attempt['state'] in {'approved', 'held', 'cancelled'}:
            return None
        if set(lanes) != set(attempt['roster']):
            raise ValueError('whole pinned roster required')
        paths = [str(Path(v['workspace']).resolve()) for v in lanes.values()]
        if len(set(paths)) != len(paths):
            raise ValueError('separate lane workspaces required')
        if conn.execute("SELECT 1 FROM review_actions WHERE attempt_id=? AND state IN ('running','reserved')", (attempt['id'],)).fetchone():
            raise ValueError('quiescent boundary required')
        ordinal = attempt['completed_rounds'] + 1
        prior = conn.execute('SELECT * FROM review_rounds WHERE attempt_id=? AND ordinal=? ORDER BY recovery DESC LIMIT 1', (attempt['id'],ordinal)).fetchone()
        if recovery:
            if not prior or prior['state'] != 'invalid':
                raise ValueError('recovery can only replace an invalid cohort')
            if attempt['recovery_used'] >= attempt['policy']['recovery']:
                state.hold(conn, attempt, 'recovery_exhausted')
                return None
        else:
            if prior or attempt['state'] != 'preflight':
                raise ValueError('new round requires primary preflight')
            preflight = conn.execute("SELECT 1 FROM review_actions WHERE attempt_id=? AND ordinal=? AND category='preflight' AND state='complete'", (attempt['id'],ordinal)).fetchone()
            if not preflight:
                raise ValueError('successful preflight receipt required')
        if ordinal > attempt['policy']['rounds']:
            state.hold(conn, attempt, 'review_exhausted')
            return None
        used = attempt['recovery_used'] + int(recovery)
        round_id = str(uuid.uuid4())
        conn.execute('INSERT INTO review_rounds VALUES(?,?,?,?,?,?,?)',
                     (round_id,attempt['id'],ordinal,used,'running',attempt['base_sha'],attempt['target_sha']))
        conn.execute("UPDATE review_attempts SET state='reviewing',recovery_used=?,version=version+1 WHERE id=?", (used,attempt['id']))
        conn.execute('UPDATE tasks SET assignee=NULL WHERE id=?', (task_id,))
        for mandate, lane in lanes.items():
            child = kb.create_task(conn, title=f'Bounded review: {mandate}',
                body=json.dumps({'attempt_id':attempt['id'],'round_id':round_id,'mandate':mandate,
                                 'base_sha':attempt['base_sha'],'target_sha':attempt['target_sha'],
                                 'spec_digest':attempt['spec_digest'],'policy_digest':attempt['policy_digest'],
                                 'owner_task_id':task_id,'evidence_is_data':True,
                                 'prior_findings':prior_findings(conn,attempt['id'],mandate)}),
                assignee=lane['profile'], workspace_kind='dir', workspace_path=lane['workspace'],
                model_override=lane['model'], provider_override=lane['provider'],
                max_runtime_seconds=attempt['policy']['active_seconds'], goal_mode=False)
            conn.execute('INSERT INTO review_members(task_id,attempt_id,round_id,mandate,state) VALUES(?,?,?,?,?)',
                         (child,attempt['id'],round_id,mandate,'reserved'))
            conn.execute('INSERT INTO review_actions(id,attempt_id,task_id,ordinal,category,recovery,state) VALUES(?,?,?,?,?,?,?)',
                         (str(uuid.uuid4()),attempt['id'],child,ordinal,'review:'+mandate,used,'reserved'))
        kb._append_event(conn, task_id, 'cohort_reserved', {'attempt_id':attempt['id'],'round_id':round_id,'ordinal':ordinal,'recovery':used})
        return round_id


def model_maker(provider, model):
    """Conservative known routing only; transport labels alone never prove maker."""
    if provider in {'anthropic', 'openrouter'} and model.startswith(('claude-', 'anthropic/claude-')):
        return 'anthropic'
    if provider in {'openai', 'openai-codex', 'openrouter'} and model.startswith(('gpt-', 'openai/gpt-', 'o3', 'o4')):
        return 'openai'
    return None


def record_runtime_route(conn, task_id, run_id, *, provider, model, isolated):
    """Launcher/request-preparation receipt, independent of reviewer output."""
    from hermes_cli import kanban_review_state as state
    with write_txn(conn):
        attempt = state.get_attempt(conn, task_id)
        current = conn.execute('SELECT current_run_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not attempt or not current or current[0] != run_id:
            raise ValueError('stale worker route')
        maker = model_maker(provider, model)
        if maker is None or maker == attempt['implementer_maker'] or not isolated:
            raise ValueError('unverified or non-independent reviewer route')
        conn.execute('UPDATE review_members SET run_id=?,provider=?,model=?,maker=?,isolated=? WHERE task_id=?',
                     (run_id,provider,model,maker,int(isolated),task_id))


def prior_findings(conn, attempt_id, mandate):
    rows=conn.execute('''SELECT m.receipt FROM review_members m JOIN review_rounds r ON r.id=m.round_id
        WHERE m.attempt_id=? AND m.mandate=? AND r.state='valid_changes' ORDER BY r.ordinal''',(attempt_id,mandate)).fetchall()
    findings={}
    for row in rows:
        for finding in json.loads(row['receipt'])['findings']:
            findings[finding['finding_id']]=finding
    return list(findings.values())


def receive(conn, task_id, run_id, receipt):
    """Inside complete_task's transaction. False means no lane completion authority."""
    from hermes_cli import kanban_review_state as state
    from hermes_cli import kanban_db as kb
    member = conn.execute('SELECT * FROM review_members WHERE task_id=?', (task_id,)).fetchone()
    if not member:
        return None
    attempt = state.get_attempt(conn, task_id)
    current = conn.execute('SELECT current_run_id FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
    if run_id is None or current != run_id or member['state'] in {'received_valid','invalid','failed'}:
        return False
    expected = {key:attempt[key] for key in ('board_id','base_sha','target_sha','policy_digest','spec_digest')}
    expected.update(attempt_id=attempt['id'],round_id=member['round_id'],mandate=member['mandate'],task_id=task_id,run_id=run_id)
    valid = isinstance(receipt, dict) and all(receipt.get(k)==v for k,v in expected.items())
    valid = valid and member['run_id']==run_id and member['maker'] not in {None,attempt['implementer_maker']} and member['isolated']==1
    if valid:
        findings = receipt.get('findings')
        valid = (receipt.get('verdict') in {'approve','request_changes'} and isinstance(findings,list)
                 and isinstance(receipt.get('verification_run'),list) and bool(receipt['verification_run'])
                 and isinstance(receipt.get('prior_findings'),list)
                 and not (receipt['verdict']=='approve' and findings)
                 and all(isinstance(f,dict) and all(f.get(k) for k in ('severity','evidence','required_change')) for f in findings)
                 and len(json.dumps(receipt)) <= 32768)
    if valid:
        previous={f['finding_id'] for f in prior_findings(conn,attempt['id'],member['mandate'])}
        closures=receipt['prior_findings']
        valid=(all(isinstance(c,dict) and c.get('finding_id') in previous
                   and c.get('status') in {'open','closed'} and c.get('evidence') for c in closures)
               and {c['finding_id'] for c in closures}==previous)
        if valid and receipt['verdict']=='approve':
            valid=all(c['status']=='closed' for c in closures)
    safe = None
    if valid:
        try:
            safe = kb.redact_review_value(receipt)
            for finding in safe['findings']:
                finding.pop('finding_id',None)
                finding['finding_id']=hashlib.sha256(json.dumps(finding,sort_keys=True).encode()).hexdigest()
        except Exception:
            valid = False
    terminal = 'received_valid' if valid else 'invalid'
    conn.execute('UPDATE review_members SET state=?,receipt=? WHERE task_id=?', (terminal,json.dumps(safe or expected),task_id))
    if not valid:
        conn.execute("UPDATE review_rounds SET state='invalid' WHERE id=?", (member['round_id'],))
        kb._append_event(conn, attempt['task_id'], 'review_invalid', {'round_id':member['round_id']}, run_id=run_id)
    aggregate(conn, attempt, member['round_id'])
    return True


def released(conn, task_id, run_id, outcome):
    member=conn.execute('SELECT * FROM review_members WHERE task_id=?',(task_id,)).fetchone()
    if not member:
        return
    if outcome!='completed':
        conn.execute("UPDATE review_members SET state='failed' WHERE task_id=? AND state NOT IN ('received_valid','invalid')",(task_id,))
    elif member['state']!='invalid':
        return
    conn.execute("UPDATE review_rounds SET state='invalid' WHERE id=? AND state='running'",(member['round_id'],))
    # A failed lane cannot leave seven native launch retries outstanding. Already
    # running lanes may finish; their receipts are history, never a mixed approval.
    conn.execute("""UPDATE review_actions SET state='cancelled' WHERE state='reserved'
        AND task_id IN (SELECT task_id FROM review_members WHERE round_id=?)""",(member['round_id'],))
    from hermes_cli import kanban_db as kb
    owner=conn.execute('SELECT task_id FROM review_attempts WHERE id=?',(member['attempt_id'],)).fetchone()[0]
    kb._append_event(conn,owner,'review_invalid',{'round_id':member['round_id']},run_id=run_id)


def aggregate(conn, attempt, round_id):
    from hermes_cli import kanban_review_state as state
    members = conn.execute('SELECT state,receipt FROM review_members WHERE round_id=?', (round_id,)).fetchall()
    if len(members) != len(attempt['roster']) or any(m['state'] != 'received_valid' for m in members):
        return
    clean = all(json.loads(m['receipt'])['verdict']=='approve' for m in members)
    changed = conn.execute("UPDATE review_rounds SET state=? WHERE id=? AND state='running'",
                           ('valid_clean' if clean else 'valid_changes',round_id)).rowcount
    if not changed:
        return
    completed = attempt['completed_rounds'] + 1
    phase = 'approved' if clean else 'repair'
    conn.execute('UPDATE review_attempts SET completed_rounds=?,state=?,version=version+1 WHERE id=?', (completed,phase,attempt['id']))
    if not clean and completed >= attempt['policy']['rounds']:
        state.hold(conn, attempt, 'review_exhausted')
    elif not clean:
        from hermes_cli import kanban_db as kb
        requested = kb._latest_event(conn, attempt['task_id'], 'review_requested')
        implementer = kb._json_dict(requested['payload']).get('implementer') if requested else None
        if not implementer:
            state.hold(conn, attempt, 'implementer_identity_missing')
            return
        conn.execute("UPDATE tasks SET status='ready',assignee=? WHERE id=?", (implementer,attempt['task_id']))
