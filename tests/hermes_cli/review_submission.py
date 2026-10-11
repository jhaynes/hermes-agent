"""Validate review metadata and print exact arguments for the agent's tool call.

Subprocesses have no Kanban mutation authority. The owning reviewer agent must
submit the printed payload verbatim with its kanban_complete tool, then read back
the canonical card. Validation here is not a receipt of board completion.
"""
import json
from pathlib import Path
import sys


def prepare_submission(metadata, manifest, journal, run_id):
    """Prepare tool arguments without reading or mutating the live board."""
    from tests.hermes_cli.review_card_receipts import validate_submission
    validation = validate_submission(metadata, manifest, journal, run_id)
    if validation['errors']:
        return validation
    return {'state': 'validated', 'errors': [], 'arguments': {
        'task_id': manifest['task_id'], 'board': manifest['board'],
        'summary': f"{manifest['lane']} review: {metadata['verdict']}",
        'metadata': metadata,
    }}


def main():
    import argparse

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
    result = prepare_submission(metadata, manifest, journal, args.run_id)
    if result['errors']:
        print(json.dumps(result, indent=2))
        return 1
    print(json.dumps(result['arguments'], indent=2))
    print('Validated only: submit this with the kanban_complete tool verbatim, '
          'then read back your card with kanban_show.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
