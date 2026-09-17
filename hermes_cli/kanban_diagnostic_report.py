"""Closed report vocabulary: claims cannot acquire authority through prose.

Facts assert only preserved event observations. Causal alternatives remain
hypotheses; this validator never certifies a person or inferred root cause.
"""
from __future__ import annotations

import json

SECTIONS = {
    'hypotheses': {'cause_unestablished', 'infrastructure_failure', 'deadline_exhaustion', 'admission_failure'},
    'contributing_conditions': {'insufficient_evidence', 'deadline_exhaustion', 'capacity_wait'},
    'missed_gates': {'not_established', 'requires_independent_reproduction'},
    'validation_needed': {'deterministic_replay', 'independent_reproduction', 'operator_decision'},
}


def valid(conn, incident, report):
    if isinstance(report, dict) and report.get('schema') == 2:
        return _valid_evidence_report(conn, incident, report)
    fields = {'incident_id', 'owner', 'citations', 'facts', 'confidence', 'confidence_basis',
              'recovery_recommendation', 'proposed_change', *SECTIONS}
    if not isinstance(report, dict) or set(report) != fields:
        return False
    if len(json.dumps(report).encode()) > 32768:
        return False
    if report['incident_id'] != incident['id'] or report['owner'] != incident['task_id']:
        return False
    citations = report['citations']
    if (not isinstance(citations, list) or not 1 <= len(citations) <= 32
            or not all(type(c) is int for c in citations)
            or len(set(citations)) != len(citations)):
        return False
    events = {}
    for source in citations:
        event = conn.execute('''SELECT e.id,e.kind,e.created_at FROM task_events e
            JOIN workflow_incident_events s ON s.event_id=e.id
            WHERE e.id=? AND e.task_id=? AND s.incident_id=?''',
            (source, incident['task_id'], incident['id'])).fetchone()
        if not event:
            return False
        events[source] = dict(event)
    facts = report['facts']
    if not isinstance(facts, list) or len(facts) > 32:
        return False
    for fact in facts:
        if not isinstance(fact, dict) or type(fact.get('id')) is not int or fact != events.get(fact['id']):
            return False
    for section, choices in SECTIONS.items():
        values = report[section]
        if not isinstance(values, list) or len(values) > 8 or any(not isinstance(v, str) or v not in choices for v in values):
            return False
    if not report['validation_needed']:
        return False
    # Confidence is explicitly about the observation, never an inferred cause.
    supported = bool(facts) and report['confidence'] == 'high' and report['confidence_basis'] == 'cited_event_observation_only'
    uncertain = report['confidence'] == 'unknown' and report['confidence_basis'] == 'cause_not_established'
    if not (supported or uncertain):
        return False
    if (not isinstance(report['recovery_recommendation'], str)
            or report['recovery_recommendation'] not in {'operator_investigation', 'operator_decision_required'}):
        return False
    from hermes_cli.kanban_workflow_lessons import admissible
    change = report['proposed_change']
    protected = (isinstance(change, dict) and set(change) == {'kind', 'approval_required'}
                 and change['approval_required'] is True and isinstance(change['kind'], str) and change['kind'] in
                 {'policy_change', 'code_change', 'limit_change', 'permission_change', 'scope_change', 'reviewer_change'})
    return change is None or admissible(change) or protected


def _valid_evidence_report(conn, incident, report):
    """Receipt observations are facts; narrative explanations never prove cause."""
    fields = {'schema', 'incident_id', 'owner', 'citations', 'facts', 'confidence', 'confidence_basis',
              'recovery_recommendation', 'proposed_change', *SECTIONS}
    if set(report) != fields or len(json.dumps(report).encode()) > 32768:
        return False
    if report['incident_id'] != incident['id'] or report['owner'] != incident['task_id']:
        return False
    from hermes_cli.kanban_diagnostic_evidence import receipts
    available = receipts(conn, incident)
    citations = report['citations']
    if (not isinstance(citations, list) or not 1 <= len(citations) <= 32
            or any(type(c) not in (str, int) or c not in available for c in citations)
            or len(set(citations)) != len(citations)):
        return False
    facts = report['facts']
    if not isinstance(facts, list) or len(facts) > 32 or any(f not in [available[c] for c in citations] for f in facts):
        return False
    for section in ('hypotheses', 'contributing_conditions', 'missed_gates'):
        entries = report[section]
        if not isinstance(entries, list) or len(entries) > 8:
            return False
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {'claim', 'citations', 'status'}
                    or entry['status'] != 'unverified' or not _text(entry['claim'])
                    or not _references(entry['citations'], citations)):
                return False
    supported = bool(facts) and report['confidence'] == 'high' and report['confidence_basis'] == 'cited_receipt_observation_only'
    unknown = report['confidence'] == 'unknown' and report['confidence_basis'] == 'cause_not_established'
    if not (supported or unknown):
        return False
    recovery = report['recovery_recommendation']
    if (not isinstance(recovery, dict) or set(recovery) != {'action', 'execution', 'citations'}
            or recovery['execution'] != 'operator_only' or not _text(recovery['action'])
            or not _references(recovery['citations'], citations)):
        return False
    validation = report['validation_needed']
    if (not isinstance(validation, list) or not 1 <= len(validation) <= 8
            or any(not isinstance(v, str) or v not in SECTIONS['validation_needed'] for v in validation)):
        return False
    change = report['proposed_change']
    if change is None:
        return True
    from hermes_cli.kanban_workflow_lessons import admissible
    if admissible(change):
        return True
    return (isinstance(change, dict) and set(change) == {'kind', 'approval_required', 'proposal'}
            and isinstance(change['kind'], str) and change['kind'] in
            {'policy_change', 'code_change', 'limit_change', 'permission_change', 'scope_change', 'reviewer_change'}
            and change['approval_required'] is True and _text(change['proposal']))


def _references(values, citations):
    return isinstance(values, list) and 1 <= len(values) <= 32 and all(v in citations for v in values)


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return False
    from hermes_cli import kanban_db as kb
    try:
        return kb.redact_review_value(value) == value
    except Exception:
        return False
