"""Park copied managed work while preserving its original receipts and budgets."""
from __future__ import annotations

import uuid


def park_snapshot(conn, *, new_identity=False):
    # Exports may originate on a pre-workflow runtime. Never initialize a second
    # schema just to copy one; normal board initialization remains its owner.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_board'").fetchone():
        return
    from hermes_cli.kanban_review_state import bind_connection
    bind_connection(conn)
    conn.execute("""UPDATE review_attempts SET state='held',
        hold_reason='transferred_snapshot_requires_operator',version=version+1
        WHERE state NOT IN ('held','cancelled')""")
    conn.execute("UPDATE review_actions SET state='failed' WHERE state='running'")
    conn.execute("UPDATE review_actions SET state='cancelled' WHERE state='reserved'")
    conn.execute("""UPDATE tasks SET status='blocked',block_kind='needs_input'
        WHERE id IN (SELECT task_id FROM workflow_postmortems UNION SELECT validator_task FROM workflow_lessons)
        AND status NOT IN ('done','archived')""")
    conn.execute("""UPDATE workflow_postmortems SET active_seconds=MAX(active_seconds,600),
        started_monotonic=NULL,deadline=0 WHERE started_monotonic IS NOT NULL""")
    conn.execute("UPDATE workflow_incidents SET report_status='synthesis_failed' WHERE report_status IN ('running','queued')")
    conn.execute("UPDATE workflow_lessons SET status='pending_approval' WHERE status='proposed'")
    conn.execute("""UPDATE tasks SET status='blocked',block_kind='needs_input'
        WHERE id IN (SELECT task_id FROM review_attempts UNION SELECT task_id FROM review_members)
        AND status NOT IN ('done','archived')""")
    if new_identity:
        # This is a writable clone, not disaster recovery of the owning board.
        # Old board ids in attempts/incidents are retained as provenance, and
        # cannot match new enrollment/admission authority.
        conn.execute('UPDATE workflow_board SET board_id=? WHERE singleton=1', (str(uuid.uuid4()),))
