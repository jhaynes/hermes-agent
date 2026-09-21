"""Finite review lifecycle CLI handlers; no model tools or parallel scheduler."""
from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.kanban_db_connect import connect_closing


def _object_file(path):
    with Path(path).open('rb') as handle:
        data=handle.read(65537)
    if len(data)>65536:
        raise ValueError('review receipt exceeds 64 KiB')
    value=json.loads(data)
    if not isinstance(value,dict):
        raise ValueError('review receipt must be an object')
    return value


def enroll(args):
    from hermes_cli.kanban_review_state import enroll_review
    receipt=_object_file(args.receipt)
    if receipt.get('operation') == 'incident':
        from hermes_cli.kanban_incident_operator import decide
        with connect_closing() as conn:
            result = decide(conn, args.task_id, receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    if receipt == {'operation':'readiness'}:
        from hermes_cli.kanban_review_readiness import begin, issue
        with connect_closing() as conn:
            result = issue(conn, begin(conn), 'cli')
        print(json.dumps(result, sort_keys=True))
        return 0
    if receipt.get('operation') == 'legacy-history':
        from hermes_cli.kanban_review_legacy import adjudicate
        with connect_closing() as conn:
            result = adjudicate(conn, args.task_id, receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    if 'operation' in receipt:
        from hermes_cli.kanban_review_operator import decide
        with connect_closing() as conn:
            result = decide(conn, args.task_id, receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    fields={'expected_status','expected_run_id','expected_assignee','board_id','spec_digest','base_sha','target_sha',
            'implementer_maker','roster','consumed','compatibility','decision'}
    if set(receipt)!=fields:
        raise ValueError('enrollment receipt fields do not match the managed review contract')
    with connect_closing() as conn:
        result=enroll_review(conn,args.task_id,**receipt)
    print(json.dumps(result,sort_keys=True))
    return 0


def reserve(args):
    from hermes_cli.kanban_review_state import reserve_action
    with connect_closing() as conn:
        action=reserve_action(conn,args.task_id,category=args.category,
                              expected_version=args.expected_version,recovery=args.recovery)
    print(json.dumps({'action_id':action}))
    return 0 if action else 1


def cohort(args):
    from hermes_cli.kanban_review_cohort import start_cohort
    lanes=_object_file(args.lanes)
    with connect_closing() as conn:
        round_id=start_cohort(conn,args.task_id,lanes=lanes,
                              expected_version=args.expected_version,recovery=args.recovery)
    print(json.dumps({'round_id':round_id}))
    return 0 if round_id else 1
