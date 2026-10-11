"""Bounded projections of the existing event/run/reviewer receipt channels."""
from __future__ import annotations

import json


def receipts(conn, incident):
    from hermes_cli import kanban_db as kb
    sources = json.loads(incident['source_events'])[:32]
    result = {}
    size = 0

    def retain(key, item):
        nonlocal size
        if len(result) >= 32:
            return
        try:
            encoded = json.dumps(item)
            if len(encoded.encode()) > 8192:
                return
            safe = _safe_projection(item, kb.redact_review_value)
            encoded = json.dumps(safe)
        except Exception:
            # Redaction failure never makes raw test output available.
            return
        if size + len(encoded.encode()) <= 16384:
            result[key] = safe
            size += len(encoded.encode())

    for event_id in sources:
        event = conn.execute('SELECT id,kind,created_at,run_id FROM task_events WHERE id=? AND task_id=?',
                             (event_id, incident['task_id'])).fetchone()
        if not event:
            continue
        retain(event_id, {key: event[key] for key in ('id', 'kind', 'created_at')})
        run = conn.execute('SELECT id,metadata FROM task_runs WHERE id=? AND task_id=?',
                           (event['run_id'], incident['task_id'])).fetchone()
        if not run or not run['metadata'] or len(run['metadata'].encode()) > 32768:
            continue
        try:
            metadata = json.loads(run['metadata'])
        except (TypeError, ValueError):
            continue
        evidence = _verification(metadata.get('verification_run')) if isinstance(metadata, dict) else []
        if evidence:
            key = f"run:{run['id']}"
            retain(key, {'citation': key, 'kind': 'run_verification', 'evidence': evidence})
    # The cohort reservation preceding the cause fixes the review snapshot;
    # a later repair/review must never rewrite an older incident's evidence.
    reserved = conn.execute('''SELECT payload FROM task_events WHERE task_id=?
        AND kind='cohort_reserved' AND id<=? ORDER BY id DESC LIMIT 1''',
        (incident['task_id'], min(sources) if sources else 0)).fetchone()
    if reserved and len(reserved['payload'] or '') <= 32768:
        try:
            round_id = json.loads(reserved['payload']).get('round_id')
        except (TypeError, ValueError, AttributeError):
            round_id = None
        if isinstance(round_id, str):
            members = conn.execute('''SELECT m.* FROM review_members m JOIN review_attempts a ON a.id=m.attempt_id
                WHERE a.task_id=? AND m.round_id=? AND m.state='received_valid' LIMIT 16''',
                (incident['task_id'], round_id))
            for member in members:
                if not member['receipt'] or len(member['receipt'].encode()) > 32768:
                    continue
                try:
                    receipt = json.loads(member['receipt'])
                except (ValueError, TypeError):
                    continue
                if not isinstance(receipt, dict) or not isinstance(receipt.get('findings'), list):
                    continue
                key = f"review:{member['task_id']}"
                retain(key, {'citation': key, 'kind': 'review_receipt', 'mandate': member['mandate'],
                             'run_id': member['run_id'], 'round_id': round_id,
                             'base_sha': receipt.get('base_sha'), 'target_sha': receipt.get('target_sha'),
                             'verdict': receipt.get('verdict'), 'evidence': _verification(receipt.get('verification_run')),
                             'findings': receipt.get('findings', [])[:8]})
    return result


def _verification(value):
    if not isinstance(value, list):
        return []
    result = []
    for entry in value[:8]:
        if not isinstance(entry, dict):
            continue
        kind = entry.get('kind')
        fields = {'executed': ('command', 'result'), 'reasoned': ('reasoning',)}.get(kind) if isinstance(kind, str) else None
        if fields and all(isinstance(entry.get(k), str) and len(entry[k]) <= 2048 for k in fields):
            result.append({'kind': entry['kind'], **{k: entry[k] for k in fields}})
    return result


def _safe_projection(value, redact):
    if isinstance(value, str):
        # Partial token masks are not idempotent and retain secret fragments.
        return value if redact(value) == value else '[redacted evidence]'
    if isinstance(value, dict):
        return {_safe_projection(key, redact): _safe_projection(item, redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_projection(item, redact) for item in value]
    return value
