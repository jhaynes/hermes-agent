"""Operator review submission through canonical Kanban tool handlers.

This is an explicit harness entrypoint, not live workflow enrollment. It must be
used instead of separately validating a preview then hand-transcribing metadata.
"""
import json
from pathlib import Path


def submit(metadata, manifest, journal, run_id, call_tool):
    """Keep validation and the exact completion payload in one submission."""
    args = {'task_id': manifest['task_id'], 'board': manifest['board']}
    card = call_tool('kanban_show', args)
    if (card.get('task', {}).get('id') != manifest['task_id']
            or card['task'].get('current_run_id') != run_id
            or card['task'].get('status') != 'running'):
        return {'state': 'invalid', 'errors': ['current card/run identity mismatch']}
    from tests.hermes_cli.review_card_receipts import validate_submission
    validation = validate_submission(metadata, manifest, journal, run_id)
    if validation['errors']:
        comment = call_tool('kanban_comment', {
            **args, 'body': json.dumps({'submission_rejected': True, 'run_id': run_id,
                                       'schema_errors': validation['errors']}),
        })
        return {**validation, 'rejection_recorded': comment.get('ok') is True}
    result = call_tool('kanban_complete', {
        **args, 'summary': f"{manifest['lane']} review: {metadata.get('verdict')}",
        'metadata': metadata,
    })
    if result.get('error'):
        return {'state': 'submission_failed', 'errors': [str(result['error'])]}
    canonical = call_tool('kanban_show', args)
    from tests.hermes_cli.review_card_receipts import ingest_card
    validation = ingest_card(canonical, manifest, journal)
    actual = canonical.get('runs', [{}])[-1].get('metadata', {})
    if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in metadata.items()):
        validation = {'state': 'invalid', 'errors': validation['errors'] + ['canonical metadata differs from submission']}
    return {**validation, 'task_id': manifest['task_id'], 'canonical_card': canonical}


def main():
    import argparse
    import os
    import subprocess

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('run_id', type=int)
    parser.add_argument('metadata', type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    metadata = json.loads(args.metadata.read_text())
    home = args.manifest.parent
    journal = [json.loads(line) for line in
               (home / f"{manifest['lane']}-receipts.jsonl").read_text().splitlines()]
    # The caller passes its canonical card/run binding explicitly because tool
    # shells can omit dispatcher variables. No force override or raw DB writes.
    for key, value in (('HERMES_KANBAN_TASK', manifest['task_id']),
                       ('HERMES_KANBAN_RUN_ID', str(args.run_id)),
                       ('HERMES_KANBAN_BOARD', manifest['board'])):
        if os.environ.get(key) not in (None, '', value):
            raise ValueError(f'conflicting {key}')
        os.environ[key] = value
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    bridge = Path(__file__).with_name('review_live_tool.py')

    def call_tool(name, payload):
        result = subprocess.run(
            [manifest['tool_python'], str(bridge), manifest['tool_runtime_root']],
            input=json.dumps({'name': name, 'args': payload}), text=True,
            capture_output=True, env=env, check=True, timeout=60,
        )
        return json.loads(result.stdout)

    result = submit(metadata, manifest, journal, args.run_id, call_tool)
    with (home / f"{manifest['lane']}-submissions.jsonl").open('a') as audit:
        audit.write(json.dumps({k: v for k, v in result.items() if k != 'canonical_card'}) + '\n')
    output = home / f"{manifest['lane']}-submission.json"
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'canonical_card'}, indent=2))
    return 0 if result['state'] == 'received_valid' else 1


if __name__ == '__main__':
    raise SystemExit(main())
