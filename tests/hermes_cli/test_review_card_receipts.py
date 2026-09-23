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
def test_submission_rejects_on_own_card_before_completion(review_receipts, tmp_path, monkeypatch, defect):
    from pathlib import Path
    import os
    import subprocess
    import sys
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from tools import kanban_tools
    from tests.hermes_cli.review_submission import submit

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setenv('HERMES_PROFILE', 'reviewtests')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title='Submission fixture', assignee='reviewtests')
        task = kb.claim_task(conn, task_id, claimer='reviewtests:1')
        assert task is not None
    monkeypatch.setenv('HERMES_KANBAN_TASK', task_id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    card, manifest, journal = review_receipts
    manifest['task_id'] = task_id
    for receipt in journal:
        receipt.update(task_id=task_id, run_id=task.current_run_id)
    good = card['runs'][0]['metadata']
    malformed = deepcopy(good)
    if defect == 'contradiction':
        malformed['findings'] = [{'severity': 'low', 'location': {'path': 'x.py', 'line': 1},
                                 'evidence': {'kind': 'reasoned', 'reasoning': 'required fix'},
                                 'required_change': 'fix it'}]
    else:
        malformed.pop({'verification': 'verification_run', 'prior': 'prior_findings',
                       'final': 'final_receipt', 'route': 'effective_route'}[defect])
    handlers = {'kanban_show': kanban_tools._handle_show,
                'kanban_complete': kanban_tools._handle_complete,
                'kanban_comment': kanban_tools._handle_comment}
    calls = []

    def tool(name, payload):
        calls.append(name)
        if defect == 'route':
            root = Path(kb.__file__).resolve().parents[1]
            result = subprocess.run(
                [sys.executable, str(root / 'tests/hermes_cli/review_live_tool.py'), str(root)],
                input=json.dumps({'name': name, 'args': payload}), text=True,
                capture_output=True, check=True, env=dict(os.environ), timeout=30,
            )
            return json.loads(result.stdout)
        return json.loads(handlers[name](payload))

    rejected = submit(malformed, manifest, journal, task.current_run_id, tool)
    assert rejected['state'] == 'invalid'
    assert 'kanban_complete' not in calls, 'schema rejection must precede board completion'
    with kbc.connect() as conn:
        assert kb.get_task(conn, task_id).status == 'running'
        assert json.loads(kb.list_comments(conn, task_id)[-1].body)['schema_errors'] == rejected['errors']
    good['verdict'] = 'request_changes'
    journal[2]['exit_code'] = 1  # failed tests are valid evidence, not schema failure
    accepted = submit(good, manifest, journal, task.current_run_id, tool)
    assert accepted['state'] == 'received_valid', accepted
    with kbc.connect() as conn:
        assert kb.get_task(conn, task_id).status == 'done'
        actual = kb.latest_run(conn, task_id).metadata
        assert {key: actual[key] for key in good} == good
