#!/usr/bin/env python3
"""Regenerate docs/contracts/seam-contract-v2.json from seam-contract-v2.md.

The prose document is the source of truth; the JSON instance is derived from it
so the two can never drift. Run from the repo root:
    python3 docs/contracts/gen_instance.py
Then recompute digests in FREEZE-MANIFEST.md.
"""
import json
import re
from pathlib import Path

CONTRACTS = Path(__file__).parent
MD = CONTRACTS / 'seam-contract-v2.md'

UNIT_CAP = {
    'U0': 'U0 contract/suite sanity',
    'U1': 'C1 configurable termination reserve',
    'U2': 'C2 verified process-group containment',
    'U3': 'C3 cohort generation/watermark export and finalization fence',
    'U4': 'C4 boot-scoped monotonic lease bounds',
    'U5': 'C5 armed/released command-release token state machine',
}
KIND_BY_ID = {
    'U2-child-grandchild': 'adversarial', 'U2-shell-wrapper': 'adversarial',
    'U2-ignored-sigterm': 'adversarial', 'U2-parent-exit': 'adversarial',
    'U2-supervisor-death-after-release': 'adversarial',
    'U2-pid-reuse-deterministic': 'adversarial',
    'U2-stale-group-reuse-deterministic': 'adversarial',
    'U2-self-group-refused': 'adversarial', 'U2-survivor-enumeration': 'adversarial',
    'U3-concurrent-claim-vs-fence': 'concurrency',
    'U3-finalization-vs-exhaustion': 'concurrency',
    'U4-half-open-start': 'boundary', 'U4-half-open-finish': 'boundary',
    'U4-exhaustion-boundary': 'boundary',
    'U5-crash-at-every-transition': 'adversarial',
    'U5-token-replay-idempotent': 'adversarial',
    'U0-contract-diff': 'environmental', 'U0-concurrency-scaffolding': 'environmental',
}
CAP_PLAN_BASIS = {
    'C1': 'plan v2:147 (launcher subtracts reserve from both enforcement boundaries), 472 (config source, two-stage validation), 474 (insufficient_safe_window)',
    'C2': 'plan v2:200 (mandatory proof cases), plan v2:23 (setsid residual accepted, detected and recorded, never claimed prevented)',
    'C3': 'plan v2:182 (watermark = authorization sequence; finalization fence), plan v2:86 (board identity anchor), plan v2:202 (native atomic admission fence; four race cases)',
    'C4': 'plan v2:145 (one boot-scoped monotonic domain; cross-boot comparisons illegal), plan v2:147 (boundary rules), plan v2:213 (timeout = min of command cap, lease remaining, allowance remaining)',
    'C5': 'plan v2:82 (durable fail-closed state machine; recovery rules), plan v2:84 (projection failure before/after release), plan v2:200 (verify/record/arm before release), plan v3 gated-launcher section (release mechanism, frozen here by reference)',
}
CAP_UNIT = {'C1': 'U1', 'C2': 'U2', 'C3': 'U3', 'C4': 'U4', 'C5': 'U5'}
CAP_CALLER = {
    'C1': ('the gated launcher (C5) and the supervisor; the config loader at kanban.review_feedback is the single source. Harness-side read lives in hermes-config Phase B slices, not the candidate.',
           'kanban_review_state.py config path; launcher/supervisor deadline construction'),
    'C2': ('the supervisor/dispatcher supervision path only. start_new_session=True is kept - it is deliberate and already gives the launcher/worker a private session and process group.',
           'kanban_db_dispatch.py spawn path; supervisor in kanban_review_state.py'),
    'C3': ('the managed-claim transaction allocates the authorization sequence; a single native finalization op is the only fence writer; the read-only export is available to the config harness/validator.',
           'kanban_review_cohort.py + managed-claim transaction in kanban_review_state.py'),
    'C4': ('the launcher (deadline construction and every comparison) and the supervisor (enforcement boundaries); every deadline comparison in the review seam is replumbed to this domain.',
           'kanban_review_state.py claimed()/settle_clock(); launcher deadline construction'),
    'C5': ('the dispatcher managed-claim path is the only actor that advances states; the gated launcher is the only executor.',
           'kanban_review_state.py action states; kanban_db_dispatch.py spawn path (gated launcher)'),
}
CAP_TYPED_FAILURES = {
    'C1': ['invalid_reserve_config', 'insufficient_safe_window', 'reserve_exceeds_boundary'],
    'C2': ['containment_identity_mismatch', 'self_group_refused', 'survivors_recorded'],
    'C3': ['cohort_claim_fenced', 'board_identity_mismatch', 'finalization_pending', 'partial_observation'],
    'C4': ['cross_boot_comparison', 'clock_reconciliation_required', 'boot_discontinuity', 'insufficient_safe_window'],
    'C5': ['crash_before_release', 'orphaned_launcher', 'projection_failure_pre_release', 'non_managed_claim_refused', 'revoked_settled'],
}
CAP_PROBES = {
    'C1': ['U1-config-validation', 'U1-insufficient-window', 'U1-boundary-subtraction', 'U1-reserve-recorded-per-action', 'U1-default-60'],
    'C2': ['U2-pgid-verified-at-arm', 'U2-child-grandchild', 'U2-shell-wrapper', 'U2-ignored-sigterm', 'U2-parent-exit',
           'U2-supervisor-death-after-release', 'U2-pid-reuse-deterministic', 'U2-stale-group-reuse-deterministic',
           'U2-self-group-refused', 'U2-survivor-enumeration'],
    'C3': ['U3-export-fields', 'U3-export-ordering', 'U3-committed-not-exported', 'U3-concurrent-claim-vs-fence',
           'U3-post-fence-claim-refused', 'U3-finalization-vs-exhaustion', 'U3-copied-db-rejected', 'U3-restore-needs-decision'],
    'C4': ['U4-boottime-domain', 'U4-half-open-start', 'U4-half-open-finish', 'U4-exhaustion-boundary',
           'U4-monotonic-decrease-hold', 'U4-boot-change-sweep', 'U4-lease-vs-exhaustion-independence'],
    'C5': ['U5-state-machine-boundaries', 'U5-crash-at-every-transition', 'U5-token-replay-idempotent',
           'U5-orphaned-launcher-recovery', 'U5-armed-no-token-recovery', 'U5-projection-failure-pre-release',
           'U5-projection-failure-post-release', 'U5-non-managed-claim-refused', 'U5-recovery-from-armed-revokes'],
}


def build():
    md = MD.read_text()

    base_probes = []
    for m in re.finditer(r'\| (BASE-[a-z0-9-]+) \| (P[0-9]+-[a-z0-9-]+) \| (.+?) \|', md):
        base_probes.append({
            'id': m.group(1), 'capability': 'baseline', 'kind': 'behavioral',
            'expected_result': m.group(3).strip(),
            'domain': 'isolated temp HERMES_HOME + temp board on tower; Linux',
            'phase_a_evidence': 'Phase A receipt probe ' + m.group(2),
        })

    u_probes = []
    for m in re.finditer(r'- \*\*(U[0-5]-[a-z0-9-]+):\*\* (.+)', md):
        pid, text = m.group(1), m.group(2).strip()
        kind = KIND_BY_ID.get(pid, 'capability')
        linux_only = pid.startswith(('U1', 'U2', 'U3', 'U4', 'U5'))
        u_probes.append({
            'id': pid, 'capability': UNIT_CAP[pid.split('-')[0]], 'kind': kind,
            'expected_result': text,
            'domain': 'isolated temp HERMES_HOME + temp board on tower; Linux-only' if linux_only
            else 'contract/tooling check, host-agnostic',
        })

    trace_rows = []
    for line in md.splitlines():
        if not line.startswith('| '):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) != 4 or cells[0] in ('plan v2 ref', '---', ':---'):
            continue
        if not (cells[0].startswith(('plan v2:', 'plan v3', 'Phase A receipt')) or
                (cells[0].startswith(':') and cells[0][1:2].isdigit())):
            continue
        if 'domain declaration' in cells[2]:
            continue
        probes = [p.strip() for p in cells[2].split(',')]
        if not all(p.startswith(('BASE-', 'U', 'P')) for p in probes):
            continue
        trace_rows.append({'plan_ref': cells[0], 'clause': cells[1], 'probes': probes, 'unit': cells[3]})

    caps = []
    for m in re.finditer(r'## (C[1-5]) — (.+?) \(unit (U[1-5])\)', md):
        cid = m.group(1)
        caps.append({
            'id': cid, 'title': m.group(2), 'plan_v2_basis': CAP_PLAN_BASIS[cid],
            'caller': {'who': CAP_CALLER[cid][0], 'module_surface': CAP_CALLER[cid][1]},
            'preconditions': _preconditions(md, cid),
            'atomic_persistence': _field(md, cid, 'Atomic persistence'),
            'generation_watermark': _field(md, cid, 'Generation/watermark') or None,
            'idempotency': _field(md, cid, 'Idempotency'),
            'typed_failures': CAP_TYPED_FAILURES[cid],
            'readback': _field(md, cid, 'Readback'),
            'probes': CAP_PROBES[cid],
            'implementation_unit': CAP_UNIT[cid],
        })

    all_ids = [p['id'] for p in base_probes + u_probes]
    dupes = {x for x in all_ids if all_ids.count(x) > 1}
    assert not dupes, f'duplicate probe ids: {dupes}'
    assert len(base_probes) == 25, f'expected 25 base probes, got {len(base_probes)}'
    assert len(u_probes) == 41, f'expected 41 U-probes, got {len(u_probes)}'
    assert len(caps) == 5, f'expected 5 capabilities, got {len(caps)}'
    known = set(all_ids)
    for row in trace_rows:
        for pid in row['probes']:
            assert pid in known, f'unknown probe in traceability: {pid}'
    for c in caps:
        for pid in c['probes']:
            assert pid in known, f'unknown probe in capability {c["id"]}: {pid}'

    return {
        'contract': {
            'name': 'seam-contract', 'version': 2, 'status': 'frozen',
            'frozen_by': 'U0 (task t_9b97698a)',
            'frozen_at_plan_sha256': '1f7cd6d13c65457581d84e8f30810549f177cdea9c2a9c18462b668183ec5e7e',
            'supersedes': 'seam-contract/1 (implicit, never frozen as a document)',
            'amendment_rule': 'Amendments are recorded as decisions with the contract version bumped and the digest recomputed. U6 judges against a specific contract SHA.',
        },
        'candidate_pin': {
            'base_commit': 'e2427daf3d7f5ef90b0dc196d89fee4a2debb0d4',
            'base_branch': 'backup/bounded-review-feedback', 'repo': 'jhaynes/hermes-agent',
            'phase_a_receipt_sha256': '78287727b9281e5d8a7ce4bb8c19da6ffc8753c8c8d455a589cff73cf199b200',
            'phase_a_verdict': 'candidate_seam_contract_failed',
            'contract_gaps_at_phase_a': [
                'C1 gap: no configurable termination_reserve_seconds (deadline hard-derived time.time()+cap, kanban_review_state.py:169)',
                'C2 gap: no verified process-group identity (group exists via start_new_session but never verified/recorded/enumerated)',
                'C3 gap: no native cohort export with generation/watermark/completeness fields or finalized-through fence',
                'C4 gap: review_actions.deadline is wall-clock, not boot-scoped monotonic with half-open not_before/expires_at',
                'C5 gap: no armed->released command-release token; arming and release are the same dispatcher tick',
            ],
        },
        'capabilities': caps,
        'probes': base_probes + u_probes,
        'traceability': trace_rows,
    }


def _section(md: str, cid: str) -> str:
    m = re.search(rf'## {cid} — .*?(?=\n## )', md, re.S)
    assert m, cid
    return m.group(0)


def _field(md: str, cid: str, name: str) -> str:
    m = re.search(rf'- \*\*{re.escape(name)}(?: \([^)]*\))?:\*\* (.+)', _section(md, cid))
    return m.group(1).strip() if m else ''


def _preconditions(md: str, cid: str) -> list:
    sec = _section(md, cid)
    m = re.search(r'- \*\*Preconditions:\*\* (.+?)(?=\n- \*\*)', sec, re.S)
    if not m:
        return []
    text = m.group(1).strip()
    return [p.strip() for p in text.split('; ') if p.strip()]


if __name__ == '__main__':
    instance = build()
    out = CONTRACTS / 'seam-contract-v2.json'
    out.write_text(json.dumps(instance, indent=2) + '\n')
    print(f'wrote {out}: probes={len(instance["probes"])} '
          f'capabilities={len(instance["capabilities"])} traceability={len(instance["traceability"])}')
