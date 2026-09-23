"""Read-only gate for operator-run review cards, not live board enrollment.

The parent supplies the pinned manifest and its runner journal independently of
reviewer prose. Structural validity is not a substitute for semantic review or
launcher-route verification. An invalid lane never discards another lane's data.
"""
import json

from hermes_cli.kanban_review_evidence import finding_valid, evidence_valid


def ingest_card(card, manifest, journal):
    """Return one lane disposition without mutating cards, counters, or receipts."""
    errors = []
    runs = card.get('runs', []) if isinstance(card, dict) else []
    task = card.get('task', {}) if isinstance(card, dict) else {}
    run = runs[-1] if isinstance(runs, list) and runs and isinstance(runs[-1], dict) else {}
    metadata = run.get('metadata')
    if not isinstance(task, dict) or not isinstance(metadata, dict):
        return {'state': 'invalid', 'errors': ['missing structured metadata']}
    if (task.get('id') != manifest['task_id'] or task.get('status') != 'done'
            or run.get('outcome') != 'completed' or type(run.get('id')) is not int):
        errors.append('card/run identity or completion mismatch')
    for key, value in (('mandate', manifest['lane']), ('base_sha', manifest['base']),
                       ('reviewed_sha', manifest['sha'])):
        if metadata.get(key) != value:
            errors.append('mismatched ' + key)
    verdict = metadata.get('verdict')
    findings = metadata.get('findings')
    if verdict not in ('approve', 'request_changes'):
        errors.append('missing verdict')
    if not isinstance(findings, list) or not all(finding_valid(f) for f in findings):
        errors.append('malformed findings')
    if verdict == 'approve' and findings:
        errors.append('approve contradicts findings')
    verification = metadata.get('verification_run')
    if not isinstance(verification, list) or not verification or not all(evidence_valid(v) for v in verification):
        errors.append('missing typed verification')
    closures = metadata.get('prior_findings')
    if not isinstance(closures, list) or not all(
        isinstance(c, dict) and isinstance(c.get('finding_id'), str)
        and c.get('status') in ('open', 'closed', 'rejected') and evidence_valid(c.get('evidence'))
        for c in closures
    ):
        errors.append('malformed prior findings')
    else:
        if {c['finding_id'] for c in closures} != set(manifest['prior_findings']) or len(closures) != len(manifest['prior_findings']):
            errors.append('missing or duplicate prior findings')
        if verdict == 'approve' and any(c['status'] == 'open' for c in closures):
            errors.append('approve leaves prior findings open')
    if not isinstance(journal, list) or len(journal) < len(manifest['commands']):
        errors.append('missing command receipts')
        journal = journal if isinstance(journal, list) else []
    previous_finish = 0
    commands = []
    for receipt in journal:
        if not isinstance(receipt, dict):
            errors.append('malformed command receipt')
            continue
        expected_identity = {'cwd': manifest['tree'], 'head': manifest['sha'], 'base': manifest['base']}
        identity = receipt.get('identity')
        if not isinstance(identity, dict) or any(identity.get(k) != v for k, v in expected_identity.items()):
            errors.append('command snapshot mismatch')
        if (receipt.get('lane') != manifest['lane'] or receipt.get('task_id') != manifest['task_id']
                or receipt.get('run_id') != run.get('id') or receipt.get('clean') is not True
                or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0):
            errors.append('command failed or belongs to another lane/run')
        started, finished = receipt.get('started'), receipt.get('finished')
        if not (type(started) in (int, float) and type(finished) in (int, float)
                and previous_finish <= started <= finished <= manifest['deadline']):
            errors.append('invalid command ordering/deadline')
        else:
            previous_finish = finished
        try:
            proof = json.loads(receipt.get('import_proof', ''))
        except (ValueError, TypeError):
            proof = None
        if not isinstance(proof, dict) or proof.get('root') != manifest['tree'] or proof.get('imports') != manifest['imports']:
            errors.append('missing pinned import proof')
        commands.append(receipt.get('command'))
    # Exact ordered commands prevent an initial identity from standing in for the
    # required final identity, or another lane's successful tests being borrowed.
    if commands != manifest['commands']:
        errors.append('required command sequence absent')
    if (not journal or not isinstance(journal[-1], dict)
            or not isinstance(metadata.get('final_receipt'), str) or not metadata['final_receipt']
            or metadata['final_receipt'] != journal[-1].get('log')):
        errors.append('missing final identity receipt reference')
    return {'state': 'invalid' if errors else 'received_valid', 'errors': errors}
