from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from resource_controller.telemetry import sample_mac
from resource_controller.lifecycle import DrainTracker
from resource_controller.storage import Store


class HostTests(unittest.TestCase):
    def test_native_sample_and_drain_never_signal_a_detached_child(self):
        # This external artifact targets Darwin; don't fake another host OS.
        self.assertEqual(sys.platform, 'darwin')
        sample = sample_mac()
        self.assertTrue(sample.valid())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'child.py'
            script.write_text('import time\ntime.sleep(1)\n')
            child = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
            tracker = DrainTracker(Store(root / 'state'))
            try:
                tracker.include([child.pid])
                self.assertFalse(tracker.drained())
                self.assertIsNone(child.poll())
                # A replacement controller must retain the same PID fingerprint.
                self.assertFalse(DrainTracker(Store(root / 'state')).drained())
                child.wait(timeout=5)
                self.assertTrue(tracker.drained())
            finally:
                child.wait(timeout=5)
