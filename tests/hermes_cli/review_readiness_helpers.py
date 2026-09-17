"""Local writer fixtures; public transport coverage lives in test_kanban_review_readiness."""
from hermes_cli.kanban_review_readiness import begin, issue


def writer_receipts(conn):
    challenge = begin(conn)
    return {'challenge_id':challenge, **{
        surface:issue(conn, challenge, surface)['id']
        for surface in ('cli','gateway','dashboard')}}
