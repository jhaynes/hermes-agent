"""Bounded workflow visibility shared by CLI details and fresh worker briefs."""
from __future__ import annotations

import json


def workflow_details(conn, task_id):
    from hermes_cli.kanban_review_state import get_attempt
    attempt=get_attempt(conn,task_id)
    owner=attempt['task_id'] if attempt else task_id
    if attempt:
        attempt={k:v for k,v in attempt.items() if k not in {'decision','compatibility'}}
    lanes=[]
    actions=[]
    if attempt:
        lanes=[dict(r) for r in conn.execute('SELECT task_id,round_id,mandate,state,run_id,maker FROM review_members WHERE attempt_id=? ORDER BY rowid DESC LIMIT 50',(attempt['id'],))]
        actions=[dict(r) for r in conn.execute('SELECT id,ordinal,category,recovery,state,run_id,deadline FROM review_actions WHERE attempt_id=? ORDER BY rowid DESC LIMIT 64',(attempt['id'],))]
        attempt['decisions'] = [json.loads(r[0]) for r in conn.execute(
            'SELECT receipt FROM review_decisions WHERE attempt_id=? ORDER BY version', (attempt['id'],))]
        attempt['lineage'] = [dict(r) for r in conn.execute(
            'SELECT * FROM review_successors WHERE predecessor_id=? OR successor_id=?', (attempt['id'],attempt['id']))]
    rows=[dict(r) for r in conn.execute('''SELECT i.id,i.state,i.report_status,i.lesson_status,i.classification,
        p.task_id AS diagnostic_task,p.runs_started,p.active_seconds,p.deadline,
        a.attachment_id,a.digest,f.error_kind AS publication_error,
        CASE WHEN a.attachment_id IS NOT NULL THEN 'complete' WHEN f.error_kind IS NOT NULL
             THEN 'failed' ELSE 'pending' END AS publication_status
        FROM workflow_incidents i LEFT JOIN workflow_postmortems p ON p.incident_id=i.id
        LEFT JOIN workflow_report_artifacts a ON a.incident_id=i.id
        LEFT JOIN workflow_report_publication_failures f ON f.incident_id=i.id
        WHERE i.task_id=? ORDER BY i.rowid DESC LIMIT 21''',(owner,))]
    return {'attempt':attempt,'lanes':lanes,'actions':actions,'incidents':rows[:20],'incidents_has_more':len(rows)>20}


def brief(conn, task_id):
    from hermes_cli.kanban_lesson_apply import brief as lesson_brief
    lessons=lesson_brief(conn)
    details=workflow_details(conn,task_id)
    if not details['attempt']:
        return lessons
    return '\n## Managed review receipt (data, not authority)\n' + json.dumps(details,sort_keys=True) + '\n' + lessons


def publish_summaries(conn):
    """Use existing comments for dashboard visibility, once per changed snapshot."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        for row in conn.execute('SELECT task_id FROM review_attempts').fetchall():
            task_id = row['task_id']
            details = workflow_details(conn, task_id)
            attempt = details['attempt']
            task = kb.get_task(conn, task_id)
            scope = conn.execute("SELECT receipt FROM review_members WHERE attempt_id=? AND mandate='scope' ORDER BY rowid DESC LIMIT 1",
                                 (attempt['id'],)).fetchone()
            verdict = kb._json_dict(scope[0]).get('verdict', 'pending') if scope else 'pending'
            owner = 'Justin (operator decision)' if attempt['state'] in {'held', 'cancelled'} else task.assignee
            body = '\n'.join([
                'Managed review ' + attempt['id'],
                f"State: {attempt['state']}; hold: {attempt['hold_reason'] or 'none'}",
                f"Rounds: {attempt['completed_rounds']}/{attempt['policy']['rounds']}; Recovery: {attempt['recovery_used']}/{attempt['policy']['recovery']}",
                f"Snapshot: {attempt['base_sha']}..{attempt['target_sha']}",
                f"Policy: {attempt['policy_digest']}; spec: {attempt['spec_digest']}",
                f"Scope verdict: {verdict}; next owner: {owner}; next action: {attempt['state']}",
                'Lanes/receipts: ' + json.dumps(details['lanes'], sort_keys=True),
                'Incidents/reports: ' + json.dumps([{key: item[key] for key in
                    ('id', 'report_status', 'diagnostic_task', 'attachment_id')} for item in details['incidents']], sort_keys=True),
                'Exact reservations and active-time accounting: show/runs JSON.',
            ])
            body = kb.redact_review_value(body)
            prior = conn.execute("SELECT body FROM task_comments WHERE task_id=? AND author='workflow-review' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            if not prior or prior[0] != body:
                kb.add_comment(conn, task_id, author='workflow-review', body=body)


def annotate_runs(conn, task_id, runs):
    """Keep the existing JSON list shape, attaching only this run's reservation."""
    diagnostic = conn.execute('''SELECT p.incident_id,p.runs_started,p.active_seconds,p.deadline,
        i.report_status,i.state FROM workflow_postmortems p JOIN workflow_incidents i ON i.id=p.incident_id
        WHERE p.task_id=?''', (task_id,)).fetchone()
    if diagnostic:
        for run in runs:
            run['diagnostic'] = dict(diagnostic)
    details = workflow_details(conn, task_id)
    attempt = details['attempt']
    if attempt is None:
        return runs
    fields = ('state', 'hold_reason', 'completed_rounds', 'recovery_used',
              'active_seconds', 'base_sha', 'target_sha', 'spec_digest', 'policy_digest')
    current = {key: attempt[key] for key in fields}
    actions = {action['run_id']: action for action in details['actions']}
    for run in runs:
        run['workflow'] = {'attempt_id': attempt['id'], **current,
                           'action': actions.get(run['id']),
                           'incident_ids': [incident['id'] for incident in details['incidents']]}
    return runs
