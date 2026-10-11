"""Operator ingest rejects malformed lanes without rewriting sibling evidence."""
from copy import deepcopy
import json

import pytest

from tests.hermes_cli.review_card_receipts import ingest_card


@pytest.fixture
def review_receipts():
    commands = [['python', 'identity.py'], ['git', 'diff', '--stat'],
                ['bash', 'scripts/run_tests.sh', '-j', '1', 'tests/example.py'], ['python', 'identity.py']]
    manifest = {'task_id': 'lane-card', 'lane': 'quality', 'base': 'b'*40,
                'sha': 'c'*40, 'tree': '/isolated/review', 'deadline': 100, 'board': 'default',
                'imports': ['/isolated/review/hermes_cli/kanban_db.py'],
                'prior_findings': ['old-finding'], 'commands': commands}
    evidence = {'kind': 'executed', 'command': 'pinned runner', 'result': 'passed'}
    metadata = {'mandate': 'quality', 'base_sha': manifest['base'], 'reviewed_sha': manifest['sha'],
                'verdict': 'approve', 'findings': [], 'verification_run': [evidence],
                'prior_findings': [{'finding_id': 'old-finding', 'status': 'closed', 'evidence': evidence}],
                'final_receipt': '/journal/3.log', 'worker_session_id': 'fixture-session',
                'effective_route': {'provider': 'fixture', 'model': 'fixture', 'maker': 'anthropic'},
                'limitations': [], 'tracking_status': 'none'}
    card = {'task': {'id': 'lane-card', 'status': 'done'},
            'runs': [{'id': 7, 'outcome': 'completed', 'metadata': metadata}]}
    journal = [{'identity': {'cwd': manifest['tree'], 'head': manifest['sha'], 'base': manifest['base']},
                'lane': 'quality', 'task_id': 'lane-card', 'run_id': 7, 'clean': True, 'exit_code': 0,
                'started': i*2+1, 'finished': i*2+2, 'command': command, 'log': f'/journal/{i}.log',
                'import_proof': json.dumps({'root': manifest['tree'], 'imports': manifest['imports']})}
               for i, command in enumerate(commands)]
    return card, manifest, journal


@pytest.mark.parametrize('defect', [
    'contradiction', 'missing_final', 'missing_test', 'claimed_final', 'wrong_sha',
    'wrong_run', 'failed_command', 'dirty', 'wrong_import', 'reordered',
    'missing_findings', 'missing_metadata', 'open_prior', 'malformed_receipt',
])
def test_invalid_lane_is_rejected_independently(review_receipts, defect):
    card, manifest, journal = review_receipts
    pristine = deepcopy(review_receipts)
    metadata = card['runs'][0]['metadata']
    finding = {'severity': 'low', 'location': {'path': 'subject.py', 'line': 1},
               'evidence': {'kind': 'reasoned', 'reasoning': 'required regression missing'},
               'required_change': 'add regression'}
    mutations = {
        'contradiction': lambda: metadata.update(findings=[finding]),
        'missing_final': lambda: journal.pop(),
        'missing_test': lambda: journal.pop(2),
        'claimed_final': lambda: metadata.update(final_receipt='/unexecuted.log'),
        'wrong_sha': lambda: metadata.update(reviewed_sha='d'*40),
        'wrong_run': lambda: journal[-1].update(run_id=6),
        'failed_command': lambda: journal[2].update(exit_code=1),
        'dirty': lambda: journal[-1].update(clean=False),
        'wrong_import': lambda: journal[2].update(import_proof=json.dumps({'root': '/primary', 'imports': []})),
        'reordered': lambda: journal.reverse(),
        'missing_findings': lambda: metadata.pop('findings'),
        'missing_metadata': lambda: card['runs'][0].update(metadata='approve'),
        'open_prior': lambda: metadata['prior_findings'][0].update(status='open'),
        'malformed_receipt': lambda: journal.__setitem__(2, None),
    }
    mutations[defect]()
    before = deepcopy(review_receipts)
    result = ingest_card(card, manifest, journal)
    assert result['state'] == 'invalid' and result['errors']
    assert review_receipts == before, 'ingest preserves original findings and receipts'
    assert ingest_card(*pristine) == {'state': 'received_valid', 'errors': []}


def test_valid_changes_remain_changes_not_clean_approval(review_receipts):
    card, manifest, journal = review_receipts
    metadata = card['runs'][0]['metadata']
    metadata['verdict'] = 'request_changes'
    metadata['findings'] = [{'severity': 'low', 'location': {'path': 'subject.py', 'line': 1},
                             'evidence': {'kind': 'reasoned', 'reasoning': 'coverage gap'},
                             'required_change': 'add regression'}]
    metadata['prior_findings'][0]['status'] = 'open'
    assert ingest_card(card, manifest, journal) == {'state': 'received_valid', 'errors': []}
    assert metadata['verdict'] == 'request_changes'


@pytest.mark.parametrize('defect', ['contradiction', 'verification', 'prior', 'final', 'route'])
def test_submission_rejects_without_board_access(review_receipts, tmp_path, monkeypatch, capsys, defect):
    card, manifest, journal = review_receipts
    malformed = deepcopy(card['runs'][0]['metadata'])
    if defect == 'contradiction':
        malformed['findings'] = [{'severity': 'low', 'location': {'path': 'x.py', 'line': 1},
                                 'evidence': {'kind': 'reasoned', 'reasoning': 'required fix'},
                                 'required_change': 'fix it'}]
    else:
        malformed.pop({'verification': 'verification_run', 'prior': 'prior_findings',
                       'final': 'final_receipt', 'route': 'effective_route'}[defect])
    invoke = _validator_cli(tmp_path, monkeypatch, manifest, journal, malformed)
    assert invoke() != 0
    output = json.loads(capsys.readouterr().out)
    from tests.hermes_cli.review_card_receipts import validate_submission
    assert output == validate_submission(malformed, manifest, journal, 7)
    assert output['errors'] and 'arguments' not in output


@pytest.mark.parametrize('verdict', ['approve', 'request_changes'])
def test_submission_prints_exact_arguments_without_board_write(review_receipts, tmp_path, monkeypatch, capsys, verdict):
    import os

    card, manifest, journal = review_receipts
    metadata = card['runs'][0]['metadata']
    metadata['verdict'] = verdict
    if verdict == 'request_changes':
        journal[2]['exit_code'] = 1  # failed tests remain evidence, not approval
    invoke = _validator_cli(tmp_path, monkeypatch, manifest, journal, metadata)
    environment = dict(os.environ)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert invoke() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        'task_id': manifest['task_id'], 'board': manifest['board'],
        'summary': f"quality review: {verdict}", 'metadata': metadata,
    }
    assert 'submit this with the kanban_complete tool' in output.err
    assert dict(os.environ) == environment, 'never alter worker authority markers'
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def _validator_cli(tmp_path, monkeypatch, manifest, journal, metadata):
    import subprocess
    import sys
    from tools import kanban_tools
    from tests.hermes_cli.review_submission import main

    def forbidden(*args, **kwargs):
        pytest.fail('validator must not invoke board handlers or subprocess bridges')

    for name in ('_handle_show', '_handle_complete', '_handle_comment'):
        monkeypatch.setattr(kanban_tools, name, forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', 'preserved-test-marker')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    manifest_path = tmp_path / 'manifest.json'
    metadata_path = tmp_path / 'review-result.json'
    manifest_path.write_text(json.dumps({**manifest, 'tool_python': sys.executable,
                                         'tool_runtime_root': str(tmp_path)}))
    metadata_path.write_text(json.dumps(metadata))
    (tmp_path / 'quality-receipts.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in journal))
    monkeypatch.setattr(sys, 'argv', ['review_submission', str(manifest_path), '7', str(metadata_path)])
    return main
