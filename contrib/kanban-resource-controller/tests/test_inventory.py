from __future__ import annotations

import unittest
from types import SimpleNamespace

from resource_controller.inventory import (
    CanonicalRun,
    IdentityHold,
    LiveWorker,
    ProcessSnapshot,
    admission_capacity,
    dispatch_maximum,
    parse_worker_argv,
    reconcile_workers,
)
from resource_controller.worker_identity import parse_stored_fingerprint
from resource_controller.spec import AdmissionCaps

EPOCH = "b3d1e2:1"
CAPS = AdmissionCaps(4, 1, (("builder", 2),), 1, (("alpha", 4),))


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
            "worker_fingerprint": parse_stored_fingerprint(f"{EPOCH}|1000"),
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
            "start_fingerprint": 1000,
        }
        values.update(overrides)
        return ProcessSnapshot(**values)  # type: ignore[arg-type]

    def reconcile(self, runs, processes):
        return reconcile_workers(runs, processes, epoch=EPOCH)

    def test_exact_identity_counts_terminal_card_worker(self) -> None:
        workers = self.reconcile([self.canonical(task_status="done")], [self.process()])
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0].task_status, "done")

    def test_pid_reuse_marker_mismatch_and_stale_rows_hold(self) -> None:
        cases = [
            ([self.canonical()], [self.process(start_fingerprint=1002)]),
            ([self.canonical()], [self.process(environment={**self.process().environment, "HERMES_KANBAN_BOARD": "other"})]),
            ([self.canonical()], []),
            ([self.canonical(), self.canonical(task_id="t_other")], [self.process()]),
            ([self.canonical()], [self.process(accessible=False)]),
            ([self.canonical()], [self.process(start_fingerprint=None)]),
        ]
        for runs, processes in cases:
            with self.subTest(runs=runs, processes=processes), self.assertRaises(IdentityHold):
                self.reconcile(runs, processes)

    def test_epoch_mismatch_holds(self) -> None:
        with self.assertRaises(IdentityHold):
            reconcile_workers([self.canonical()], [self.process()], epoch="different-epoch")

    def test_unknown_marked_process_holds(self) -> None:
        unknown = self.process(
            pid=99,
            argv=("python", "mystery.py"),
            environment={"HERMES_KANBAN_TASK": "t_x"},
        )
        with self.assertRaises(IdentityHold):
            self.reconcile([], [unknown])

    def test_exact_previous_and_ended_identity_authorizes_finishing_worker(self) -> None:
        previous = LiveWorker(
            "alpha", "t_123", 7, 42, 1000.2, "builder", "running", f"{EPOCH}|1000",
        )
        ended = SimpleNamespace(
            board="alpha", task_id="t_123", run_id=7, pid=42, profile="builder",
            worker_fingerprint=parse_stored_fingerprint(f"{EPOCH}|1000"), run_status="done",
        )
        workers = reconcile_workers(
            [], [self.process()], epoch=EPOCH, ended_runs=(ended,), previous_workers=(previous,),
        )
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0].task_status, "done")
        self.assertEqual(workers[0].worker_fingerprint, previous.worker_fingerprint)

    def test_finishing_worker_requires_board_profile_unique_pid_and_every_marker(self) -> None:
        previous = LiveWorker(
            "alpha", "t_123", 7, 42, 1000.2, "builder", "running", f"{EPOCH}|1000",
        )

        def ended(**changes):
            values = dict(
                board="alpha", task_id="t_123", run_id=7, pid=42, profile="builder",
                worker_fingerprint=parse_stored_fingerprint(f"{EPOCH}|1000"), run_status="done",
            )
            values.update(changes)
            return SimpleNamespace(**values)

        cases = [
            ((ended(board="other"),), (previous,), self.process()),
            ((ended(profile="reviewer"),), (previous,), self.process()),
            ((ended(),), (previous, previous), self.process()),
        ]
        for marker in (
            "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD", "HERMES_PROFILE",
        ):
            environment = dict(self.process().environment)
            environment[marker] = "wrong"
            cases.append(((ended(),), (previous,), self.process(environment=environment)))
        for ended_runs, previous_workers, process in cases:
            with self.subTest(ended=ended_runs, environment=process.environment), self.assertRaises(IdentityHold):
                reconcile_workers(
                    [], [process], epoch=EPOCH,
                    ended_runs=ended_runs, previous_workers=previous_workers,
                )

    def test_capacity_is_host_profile_and_board_admission_only(self) -> None:
        one = self.reconcile([self.canonical()], [self.process()])
        self.assertEqual(admission_capacity(one, caps=CAPS, board="beta", profile="reviewer").reason, "available")
        self.assertEqual(admission_capacity(one, caps=CAPS, board="beta", profile="builder").reason, "available")
        self.assertEqual(admission_capacity(one, caps=CAPS, board="alpha", profile="reviewer").reason, "available")

        builder_two = LiveWorker("beta", "t_2", 8, 43, 2.0, "builder", "done")
        self.assertEqual(
            admission_capacity((*one, builder_two), caps=CAPS, board="gamma", profile="builder").reason,
            "profile-cap",
        )
        alpha_four = tuple(
            LiveWorker("alpha", f"t_{index}", index, index, float(index), f"p{index}", "running")
            for index in range(4)
        )
        self.assertEqual(
            admission_capacity(alpha_four, caps=AdmissionCaps(5, 5, (), 1, (("alpha", 4),)), board="alpha", profile="new").reason,
            "board-cap",
        )

        second_run = self.canonical(
            board="beta", task_id="t_2", run_id=8, pid=43, profile="reviewer",
            worker_fingerprint=parse_stored_fingerprint(f"{EPOCH}|2000"),
        )
        second_process = self.process(
            pid=43,
            argv=("hermes", "-p", "reviewer", "--cli", "--accept-hooks", "chat", "-q", "work kanban task t_2"),
            environment={
                "HERMES_KANBAN_TASK": "t_2", "HERMES_KANBAN_RUN_ID": "8",
                "HERMES_KANBAN_BOARD": "beta", "HERMES_PROFILE": "reviewer",
            },
            start_fingerprint=2000,
        )
        workers = self.reconcile([self.canonical(), second_run], [self.process(), second_process])
        four = (*workers,
            LiveWorker("delta", "t_3", 9, 44, 3.0, "p3", "running"),
            LiveWorker("epsilon", "t_4", 10, 45, 4.0, "p4", "running"),
        )
        self.assertEqual(admission_capacity(workers, caps=CAPS, board="gamma", profile="default").reason, "available")
        self.assertEqual(admission_capacity(four, caps=CAPS, board="gamma", profile="default").reason, "host-cap")

    def test_dispatch_maximum_uses_selected_board_database_running_count(self) -> None:
        self.assertEqual(dispatch_maximum(CAPS, board="alpha", hermes_db_running_count=0), 1)
        self.assertEqual(dispatch_maximum(CAPS, board="alpha", hermes_db_running_count=1), 2)
        self.assertEqual(dispatch_maximum(CAPS, board="alpha", hermes_db_running_count=2), 3)
        self.assertEqual(dispatch_maximum(CAPS, board="alpha", hermes_db_running_count=3), 4)
        self.assertIsNone(dispatch_maximum(CAPS, board="alpha", hermes_db_running_count=4))
        self.assertEqual(
            dispatch_maximum(CAPS, board="beta", hermes_db_running_count=0),
            1,
            "blanket board cap remains independent of the alpha override",
        )


if __name__ == "__main__":
    unittest.main()
