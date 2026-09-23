from __future__ import annotations

import unittest

from resource_controller.inventory import (
    CanonicalRun,
    IdentityHold,
    ProcessSnapshot,
    admission_capacity,
    parse_worker_argv,
    reconcile_workers,
)


class WorkerArgvTests(unittest.TestCase):
    def test_exact_worker_command_is_parsed_without_substring_matching(self) -> None:
        parsed = parse_worker_argv(
            [
                "/opt/hermes/bin/python", "/opt/hermes/hermes.py", "-p", "builder",
                "--cli", "--accept-hooks", "--skills", "github", "-m", "model",
                "--provider", "provider", "chat", "-q", "work kanban task t_123",
            ]
        )
        self.assertEqual(parsed.profile, "builder")
        self.assertEqual(parsed.task_id, "t_123")

    def test_similar_or_malformed_commands_are_not_workers(self) -> None:
        bad = [
            ["python", "script.py", "serve", "work kanban task t_1"],
            ["hermes", "-p", "builder", "chat", "-q", "work kanban task t_1", "extra"],
            ["hermes", "-p", "builder", "--cli", "chat", "-q", "work kanban task"],
            ["hermes", "-p", "builder", "--cli", "chat", "-q", "work kanban task ../bad"],
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(IdentityHold):
                parse_worker_argv(argv)


class ReconciliationTests(unittest.TestCase):
    def canonical(self, **overrides: object) -> CanonicalRun:
        values = {
            "board": "alpha",
            "task_id": "t_123",
            "run_id": 7,
            "pid": 42,
            "worker_started_at": 1000,
            "profile": "builder",
            "task_status": "running",
        }
        values.update(overrides)
        return CanonicalRun(**values)  # type: ignore[arg-type]

    def process(self, **overrides: object) -> ProcessSnapshot:
        values = {
            "pid": 42,
            "created_at": 1000.2,
            "ppid": 1,
            "argv": ("hermes", "-p", "builder", "--cli", "--accept-hooks", "chat", "-q", "work kanban task t_123"),
            "environment": {
                "HERMES_KANBAN_TASK": "t_123",
                "HERMES_KANBAN_RUN_ID": "7",
                "HERMES_KANBAN_BOARD": "alpha",
                "HERMES_PROFILE": "builder",
            },
            "accessible": True,
        }
        values.update(overrides)
        return ProcessSnapshot(**values)  # type: ignore[arg-type]

    def test_exact_identity_counts_terminal_card_worker(self) -> None:
        workers = reconcile_workers([self.canonical(task_status="done")], [self.process()])
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0].task_status, "done")

    def test_pid_reuse_marker_mismatch_and_stale_rows_hold(self) -> None:
        cases = [
            ([self.canonical()], [self.process(created_at=1002.0)]),
            ([self.canonical()], [self.process(environment={**self.process().environment, "HERMES_KANBAN_BOARD": "other"})]),
            ([self.canonical()], []),
            ([self.canonical(), self.canonical(task_id="t_other")], [self.process()]),
            ([self.canonical()], [self.process(accessible=False)]),
        ]
        for runs, processes in cases:
            with self.subTest(runs=runs, processes=processes), self.assertRaises(IdentityHold):
                reconcile_workers(runs, processes)

    def test_unknown_marked_process_holds(self) -> None:
        unknown = self.process(
            pid=99,
            argv=("python", "mystery.py"),
            environment={"HERMES_KANBAN_TASK": "t_x"},
        )
        with self.assertRaises(IdentityHold):
            reconcile_workers([], [unknown])

    def test_capacity_is_host_profile_and_board_admission_only(self) -> None:
        one = reconcile_workers([self.canonical()], [self.process()])
        self.assertEqual(admission_capacity(one, board="beta", profile="reviewer").reason, "available")
        self.assertEqual(admission_capacity(one, board="beta", profile="builder").reason, "profile-cap")
        self.assertEqual(admission_capacity(one, board="alpha", profile="reviewer").reason, "board-cap")

        second_run = self.canonical(board="beta", task_id="t_2", run_id=8, pid=43, profile="reviewer")
        second_process = self.process(
            pid=43,
            argv=("hermes", "-p", "reviewer", "--cli", "--accept-hooks", "chat", "-q", "work kanban task t_2"),
            environment={
                "HERMES_KANBAN_TASK": "t_2", "HERMES_KANBAN_RUN_ID": "8",
                "HERMES_KANBAN_BOARD": "beta", "HERMES_PROFILE": "reviewer",
            },
        )
        workers = reconcile_workers([self.canonical(), second_run], [self.process(), second_process])
        self.assertEqual(admission_capacity(workers, board="gamma", profile="default").reason, "host-cap")


if __name__ == "__main__":
    unittest.main()
