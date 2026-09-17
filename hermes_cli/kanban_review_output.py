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
