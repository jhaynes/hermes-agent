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
        from hermes_cli.kanban_review_guards import has_live_worker
        if has_live_worker(conn, attempt['id']) or conn.execute("SELECT 1 FROM review_actions WHERE attempt_id=? AND state IN ('running','reserved')", (attempt['id'],)).fetchone():
            raise ValueError('quiescent boundary required')
        ordinal = attempt['completed_rounds'] + 1
        route = conn.execute('''SELECT e.payload FROM task_events e
            JOIN review_actions a ON a.run_id=e.run_id AND a.task_id=e.task_id
            WHERE a.attempt_id=? AND a.ordinal=? AND a.category='preflight'
              AND a.state='complete' AND e.kind='runtime_route_verified'
            ORDER BY e.id DESC LIMIT 1''', (attempt['id'], ordinal)).fetchone()
        receipt = json.loads(route[0]) if route else {}
        expected = {key: attempt[key] for key in
                    ('board_id', 'spec_digest', 'policy_digest', 'base_sha', 'target_sha')}
        expected.update(attempt_id=attempt['id'], task_id=task_id, maker=attempt['implementer_maker'])
        if (not all(receipt.get(k) == v for k, v in expected.items())
                or model_maker(receipt.get('provider'), receipt.get('model', '')) != attempt['implementer_maker']):
            raise ValueError('verified implementer runtime receipt for this preflight required')
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
        from hermes_cli.kanban_review_operator import approved_ask
        ask = kb.redact_review_value(approved_ask(conn, attempt))
        for mandate, lane in lanes.items():
            child = kb.create_task(conn, title=f'Bounded review: {mandate}',
                body=json.dumps({'attempt_id':attempt['id'],'round_id':round_id,'mandate':mandate,
                                 'base_sha':attempt['base_sha'],'target_sha':attempt['target_sha'],
                                 'spec_digest':attempt['spec_digest'],'policy_digest':attempt['policy_digest'],
                                 'owner_task_id':task_id,'evidence_is_data':True,
                                 'approved_ask':ask,
                                 'receipt_contract':{
                                     'metadata_key':'bounded_review',
                                     'identity':'Echo the pinned board/attempt/round/task/run/mandate and base/target/spec/policy fields.',
                                     'verdict':['approve','request_changes'],
                                     'evidence_kinds':['executed','reasoned'],
                                     'executed_evidence':{'kind':'executed','command':'actual command','result':'observed result'},
                                     'reasoned_evidence':{'kind':'reasoned','reasoning':'explicitly unexecuted analysis'},
                                     'finding_fields':['severity','location','evidence','required_change'],
                                     'severity':['critical','high','medium','low'],
                                     'location':{'path':'repository-relative path','line':'positive integer'},
                                     'verification_run':'Nonempty list of typed evidence objects.',
                                     'prior_findings':'One finding_id/status/evidence entry per prior finding; rejected requires parent disposition.',
                                     'scope':'Scope lane must supply mapping (requirement/change/typed evidence), missing_evidence and extraneous lists, and safety_dispositions (change/requirement/rationale/disposition/follow_up). Necessary safety stays tied to the ask; out_of_scope needs an explicit follow-up. Approval cannot leave missing evidence or extraneous changes.',
                                 },
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
        if task_id == attempt['task_id']:
            if maker is None or maker != attempt['implementer_maker']:
                raise ValueError('implementer route differs from enrolled maker')
            from hermes_cli import kanban_db as kb
            receipt = {key: attempt[key] for key in ('board_id', 'spec_digest', 'policy_digest', 'base_sha', 'target_sha')}
            receipt.update(attempt_id=attempt['id'], task_id=task_id, run_id=run_id,
                           provider=provider, model=model, maker=maker)
            old = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='runtime_route_verified' LIMIT 1", (task_id, run_id)).fetchone()
            if old and json.loads(old[0]) != receipt:
                raise ValueError('implementer route differs from pinned runtime receipt')
            if not old:
                kb._append_event(conn, task_id, 'runtime_route_verified', receipt, run_id=run_id)
            return
        if maker is None or maker == attempt['implementer_maker'] or not isolated:
            raise ValueError('unverified or non-independent reviewer route')
        conn.execute('UPDATE review_members SET run_id=?,provider=?,model=?,maker=?,isolated=? WHERE task_id=?',
                     (run_id,provider,model,maker,int(isolated),task_id))


def prior_findings(conn, attempt_id, mandate):
    rows=conn.execute('''WITH RECURSIVE lineage(id) AS (
        SELECT ? UNION SELECT s.predecessor_id FROM review_successors s JOIN lineage l ON s.successor_id=l.id)
        SELECT m.receipt FROM review_members m JOIN review_rounds r ON r.id=m.round_id
        WHERE m.attempt_id IN (SELECT id FROM lineage) AND m.mandate=?
        AND r.state='valid_changes' ORDER BY r.rowid''',(attempt_id,mandate)).fetchall()
    findings={}
    for row in rows:
        for finding in json.loads(row['receipt'])['findings']:
            findings[finding['finding_id']]=finding
    for finding in findings.values():
        disposition = conn.execute('SELECT receipt FROM review_dispositions WHERE finding_id=?', (finding['finding_id'],)).fetchone()
        if disposition:
            finding['disposition'] = json.loads(disposition[0])
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
        from hermes_cli.kanban_review_evidence import receipt_evidence_valid
        findings = receipt.get('findings')
        valid = (receipt.get('verdict') in {'approve','request_changes'} and isinstance(findings,list)
                 and receipt_evidence_valid(receipt)
                 and not (receipt['verdict']=='approve' and findings)
                 and len(json.dumps(receipt)) <= 32768)
    if valid:
        prior = prior_findings(conn,attempt['id'],member['mandate'])
        previous={f['finding_id'] for f in prior}
        rejected={f['finding_id'] for f in prior if f.get('disposition',{}).get('status')=='rejected'}
        closures=receipt['prior_findings']
        valid=(all(isinstance(c,dict) and c.get('finding_id') in previous
                   and (c.get('status') in {'open','closed'} or (c.get('status')=='rejected' and c['finding_id'] in rejected))
                   and c.get('evidence') for c in closures)
               and {c['finding_id'] for c in closures}==previous)
        if valid and receipt['verdict']=='approve':
            valid=all(c['status'] in {'closed','rejected'} for c in closures)
    safe = None
    if valid:
        try:
            safe = kb.redact_review_value(receipt)
            for finding in safe['findings']:
                finding['finding_id']=hashlib.sha256(json.dumps([expected, finding],sort_keys=True).encode()).hexdigest()
                finding['provenance'] = expected
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
    current = state.get_attempt(conn, attempt['task_id'])
    if (not current or current['id'] != attempt['id'] or current['state'] != 'reviewing'
            or current['version'] != attempt['version']):
        return
    round_row = conn.execute('SELECT * FROM review_rounds WHERE id=?', (round_id,)).fetchone()
    if (not round_row or round_row['attempt_id'] != current['id']
            or round_row['ordinal'] != current['completed_rounds'] + 1
            or round_row['base_sha'] != current['base_sha']
            or round_row['target_sha'] != current['target_sha']):
        return
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
