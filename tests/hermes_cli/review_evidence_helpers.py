"""Typed scope evidence for synthetic lifecycle fixtures, not a real review."""


def scope_evidence(evidence=None):
    return {'mapping': [{'requirement': 'synthetic approved ask',
                         'change': 'synthetic target snapshot',
                         'evidence': evidence or {'kind': 'reasoned', 'reasoning': 'Synthetic lifecycle fixture; no code review claimed.'}}],
            'missing_evidence': [], 'extraneous': [], 'safety_dispositions': []}
