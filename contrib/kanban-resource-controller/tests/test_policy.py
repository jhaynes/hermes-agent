import unittest

from resource_controller.policy import Gate, Sample


class PolicyTests(unittest.TestCase):
    def test_admission_requires_quiet_contiguous_dwell_and_restarts_after_action(self):
        gate = Gate()
        def sample(t, **kw):
            return gate.observe(Sample(t, 10, kw.get('load', 8), kw.get('available', 5 * 2**30),
                                       kw.get('pressure', 1), kw.get('sin', 100), kw.get('sout', 200)))
        self.assertEqual(sample(0), 'resource:cold')
        for t in (30, 60, 90, 120):
            self.assertEqual(sample(t), 'resource:dwell')
        self.assertEqual(sample(150), 'eligible')
        gate.reset()
        self.assertEqual(sample(180), 'resource:cold')
        self.assertEqual(sample(210, load=10), 'resource:load')
        self.assertEqual(sample(240, available=4 * 2**30 - 1), 'resource:memory')
        self.assertEqual(sample(270, pressure=2), 'resource:pressure')
        self.assertEqual(sample(300, sin=101), 'resource:paging-in')
        self.assertEqual(sample(330, sin=101, sout=201), 'resource:paging-out')
        self.assertEqual(sample(360), 'resource:counter-reset')
        self.assertEqual(sample(500), 'resource:gap')
        self.assertEqual(sample(530, load=9), 'resource:recovery-band')
        self.assertEqual(gate.observe(None), 'resource:unknown')
        self.assertEqual(sample(560), 'resource:cold')
        self.assertEqual(sample(590, load=float('nan')), 'resource:unknown')


if __name__ == '__main__':
    unittest.main()
