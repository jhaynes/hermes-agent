from __future__ import annotations

import unittest
import unittest.mock

from resource_controller import processes
from resource_controller.worker_identity import IdentityUnstable


class StableIdentityTests(unittest.TestCase):
    """The start fingerprint must bracket the argv/env collection (MoA v1 decision 4)."""

    def test_reads_start_before_and_after_collection(self) -> None:
        calls: list[str] = []

        def start(pid: int) -> int:
            calls.append("start")
            return 179027411681

        def collect() -> dict:
            calls.append("collect")
            return {"HERMES_KANBAN_TASK": "t_x"}

        value, collected = processes._stable_identity(42, collect, start)
        self.assertEqual(value, 179027411681)
        self.assertEqual(collected, {"HERMES_KANBAN_TASK": "t_x"})
        self.assertEqual(calls, ["start", "collect", "start"])

    def test_one_recapture_recollects_and_returns_the_stable_second_attempt(self) -> None:
        starts = iter([100, 101, 101, 101])
        collections = iter([{"n": 1}, {"n": 2}])
        value, collected = processes._stable_identity(
            42, lambda: next(collections), lambda pid: next(starts)
        )
        self.assertEqual(value, 101)
        self.assertEqual(collected, {"n": 2})

    def test_two_unstable_attempts_raise_identity_unstable(self) -> None:
        starts = iter([100, 101, 102, 103, 104])
        with self.assertRaises(IdentityUnstable):
            processes._stable_identity(42, lambda: {}, lambda pid: next(starts))

    def test_unreadable_start_both_times_is_returned_as_none_for_inventory_to_hold(self) -> None:
        value, _ = processes._stable_identity(42, lambda: {}, lambda pid: None)
        self.assertIsNone(value)


class _FakeProcess:
    def __init__(self, pid: int, argv: tuple[str, ...], env: dict, uid: int) -> None:
        self.pid = pid
        self.info = {
            "pid": pid, "ppid": 1, "create_time": 1790274116.812699,
            "cmdline": list(argv), "uids": unittest.mock.Mock(real=uid),
        }
        self._env = env
        self.environ_calls = 0

    def environ(self) -> dict:
        self.environ_calls += 1
        return dict(self._env)


class ScanWorkerProcessesTests(unittest.TestCase):
    ARGV = (
        "/venv/bin/python3", "-m", "hermes_cli.main", "-p", "reviewquality", "--cli",
        "--accept-hooks", "chat", "-q", "work kanban task t_c9f3e704",
    )

    def test_scan_brackets_environ_between_two_start_reads(self) -> None:
        import os

        proc = _FakeProcess(12936, self.ARGV, {"HERMES_KANBAN_TASK": "t_c9f3e704"}, os.getuid())
        order: list[str] = []
        real_environ = proc.environ

        def environ() -> dict:
            order.append("environ")
            return real_environ()

        proc.environ = environ  # type: ignore[method-assign]

        def start(pid: int) -> int:
            order.append("start")
            return 179027411681

        try:
            processes.parse_worker_argv(self.ARGV)
        except Exception as exc:  # pragma: no cover - fixture must match the real argv parser
            self.fail(f"fixture argv not accepted by parse_worker_argv: {exc}")
        snapshots = processes.scan_worker_processes(
            process_iter=lambda attrs: [proc], start_fn=start
        )
        self.assertEqual(order, ["start", "environ", "start"])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].start_fingerprint, 179027411681)
        self.assertEqual(snapshots[0].environment, {"HERMES_KANBAN_TASK": "t_c9f3e704"})


if __name__ == "__main__":
    unittest.main()
