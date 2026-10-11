"""Typed review evidence; reasoning never masquerades as an executed probe."""
from pathlib import PurePosixPath


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 8192


def evidence_valid(value):
    if not isinstance(value, dict):
        return False
    if value.get('kind') == 'executed':
        return set(value) == {'kind', 'command', 'result'} and _text(value['command']) and _text(value['result'])
    if value.get('kind') == 'reasoned':
        return set(value) == {'kind', 'reasoning'} and _text(value['reasoning'])
    return False


def finding_valid(value):
    if not isinstance(value, dict) or set(value) != {'severity', 'location', 'evidence', 'required_change'}:
        return False
    location = value['location']
    if not isinstance(location, dict) or set(location) != {'path', 'line'}:
        return False
    path = location['path']
    return (isinstance(value['severity'], str) and value['severity'] in {'critical', 'high', 'medium', 'low'}
            and _text(path) and not PurePosixPath(path).is_absolute()
            and '..' not in PurePosixPath(path).parts and '\\' not in path
            and type(location['line']) is int and location['line'] > 0
            and evidence_valid(value['evidence']) and _text(value['required_change']))


def receipt_evidence_valid(receipt):
    findings = receipt.get('findings')
    probes = receipt.get('verification_run')
    closures = receipt.get('prior_findings')
    if not (isinstance(findings, list) and isinstance(probes, list) and probes
            and isinstance(closures, list)):
        return False
    return ((receipt.get('mandate') != 'scope' or scope_valid(receipt))
            and all(finding_valid(f) for f in findings)
            and all(evidence_valid(probe) for probe in probes)
            and all(isinstance(c, dict) and set(c) == {'finding_id', 'status', 'evidence'}
                    and _text(c['finding_id']) and isinstance(c['status'],str) and c['status'] in {'open', 'closed', 'rejected'}
                    and evidence_valid(c['evidence']) for c in closures)
            and len({c['finding_id'] for c in closures}) == len(closures))


def scope_valid(receipt):
    scope = receipt.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'mapping', 'missing_evidence', 'extraneous', 'safety_dispositions'}:
        return False
    mapping = scope['mapping']
    if not isinstance(mapping, list) or not mapping or len(mapping) > 64:
        return False
    if not all(isinstance(item, dict) and set(item) == {'requirement', 'change', 'evidence'}
               and _text(item['requirement']) and _text(item['change']) and evidence_valid(item['evidence'])
               for item in mapping):
        return False
    for field in ('missing_evidence', 'extraneous'):
        if not isinstance(scope[field], list) or not all(_text(item) for item in scope[field]):
            return False
    dispositions = scope['safety_dispositions']
    if not isinstance(dispositions, list) or not all(
            isinstance(item, dict) and set(item) == {'change', 'requirement', 'rationale', 'disposition', 'follow_up'}
            and all(_text(item[key]) for key in ('change', 'requirement', 'rationale'))
            and item['disposition'] in ('necessary_safety', 'out_of_scope')
            and (item['follow_up'] is None if item['disposition'] == 'necessary_safety' else _text(item['follow_up']))
            for item in dispositions):
        return False
    return receipt['verdict'] != 'approve' or not (scope['missing_evidence'] or scope['extraneous'])
