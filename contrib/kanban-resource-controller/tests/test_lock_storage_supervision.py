from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

from resource_controller.locking import LockContended, SingletonLock
from resource_controller.storage import SecureStateStore, StorageError
from resource_controller.supervision import run_supervised


class SingletonLockTests(unittest.TestCase):
    def test_one_owner_noninheritable_and_recovery_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "dispatcher.lock"
            first = SingletonLock(path)
            first.acquire()
            self.assertFalse(os.get_inheritable(first.fileno()))
            second = SingletonLock(path)
            with self.assertRaises(LockContended):
                second.acquire()
            first.release()
            second.acquire()
            second.release()
            self.assertTrue(path.exists(), "lock inode must never be unlinked")


class SecureStorageTests(unittest.TestCase):
    def test_atomic_json_uses_restrictive_modes_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_dir = Path(root) / "state"
            store = SecureStateStore(state_dir)
            store.write_json("status.json", {"state": "hold", "count": 2})
            self.assertEqual(store.read_json("status.json"), {"state": "hold", "count": 2})
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((state_dir / "status.json").stat().st_mode), 0o600)

    def test_symlink_and_unknown_pending_journal_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "target"
            target.mkdir()
            linked = Path(root) / "linked"
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaises(StorageError):
                SecureStateStore(linked)

            store = SecureStateStore(Path(root) / "state")
            store.write_json("pending.json", {"command": ["hermes"], "outcome": "pending"})
            self.assertTrue(store.has_pending_uncertainty())
            store.write_json("pending.json", {"command": ["hermes"], "outcome": "reconciled"})
            self.assertFalse(store.has_pending_uncertainty())


class SupervisionTests(unittest.TestCase):
    def test_timeout_observes_without_killing_and_waits_for_finite_child(self) -> None:
        started = time.monotonic()
        result = run_supervised(
            [sys.executable, "-c", "import time; time.sleep(0.12); print('done')"],
            observe_timeout=0.02,
            max_output=1024,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "done")
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_nonzero_and_oversized_output_are_uncertain(self) -> None:
        nonzero = run_supervised(
            [sys.executable, "-c", "import sys; print('bad'); sys.exit(4)"],
            observe_timeout=1,
            max_output=1024,
        )
        self.assertTrue(nonzero.uncertain)
        self.assertEqual(nonzero.returncode, 4)

        oversized = run_supervised(
            [sys.executable, "-c", "print('x' * 200)"],
            observe_timeout=1,
            max_output=64,
        )
        self.assertTrue(oversized.uncertain)
        self.assertTrue(oversized.truncated)
        self.assertLessEqual(len(oversized.stdout.encode()), 64)


if __name__ == "__main__":
    unittest.main()
