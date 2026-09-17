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
    return (all(finding_valid(f) for f in findings)
            and all(evidence_valid(probe) for probe in probes)
            and all(isinstance(c, dict) and set(c) == {'finding_id', 'status', 'evidence'}
                    and _text(c['finding_id']) and isinstance(c['status'],str) and c['status'] in {'open', 'closed', 'rejected'}
                    and evidence_valid(c['evidence']) for c in closures)
            and len({c['finding_id'] for c in closures}) == len(closures))
