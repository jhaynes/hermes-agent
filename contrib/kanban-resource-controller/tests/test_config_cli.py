from __future__ import annotations

import json
from pathlib import Path
import unittest

from resource_controller.cli_contract import (
    CommandContractError,
    build_decompose_command,
    build_dispatch_command,
    build_dry_run_command,
    parse_dispatch_prediction,
)
from resource_controller.config import ConfigCompatibilityError, ControllerConfig
from resource_controller.spec import AdmissionCaps


CAPS = AdmissionCaps(4, 1, (("builder", 2),), 1, (("smithers", 4),))


class ConfigContractTests(unittest.TestCase):
    def valid(self) -> dict:
        return {
            "dispatch_in_gateway": False,
            "max_in_progress": 4,
            "max_in_progress_per_profile": 2,
            "failure_limit": 2,
            "auto_decompose": True,
            "reconcile_orphans": True,
            "dispatch_stale_timeout_seconds": 0,
            "review_dispatch": True,
            "default_assignee": None,
            "dispatch_profiles": ["builder", "reviewscope"],
        }

    def test_exact_compatible_config_is_accepted(self) -> None:
        cfg = ControllerConfig.from_mapping(self.valid(), admission_caps=CAPS)
        self.assertEqual(cfg.failure_limit, 2)
        self.assertTrue(cfg.auto_decompose)

    def test_safety_and_cli_parity_mismatches_are_refused_individually(self) -> None:
        wrong_values = {
            "dispatch_in_gateway": True,
            "max_in_progress": 3,
            "max_in_progress_per_profile": 1,
            "failure_limit": 0,
            "auto_decompose": "true",
            "reconcile_orphans": False,
            "dispatch_stale_timeout_seconds": 14400,
            "review_dispatch": "yes",
            "default_assignee": 7,
            "dispatch_profiles": "builder",
        }
        for key, value in wrong_values.items():
            with self.subTest(key=key):
                raw = self.valid()
                raw[key] = value
                with self.assertRaisesRegex(ConfigCompatibilityError, key):
                    ControllerConfig.from_mapping(raw, admission_caps=CAPS)

    def test_caps_and_explicit_profile_registry_must_match(self) -> None:
        mutations = (
            ("max_in_progress", 3),
            ("max_in_progress", 5),
            ("max_in_progress_per_profile", 1),
            ("max_in_progress_per_profile", 3),
            ("dispatch_profiles", None),
            ("dispatch_profiles", []),
            ("dispatch_profiles", ["builder", "builder"]),
            ("dispatch_profiles", ["reviewscope"]),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                raw = self.valid()
                raw[key] = value
                with self.assertRaisesRegex(ConfigCompatibilityError, key):
                    ControllerConfig.from_mapping(raw, admission_caps=CAPS)

    def test_empty_profile_registry_is_refused_without_profile_overrides(self) -> None:
        caps = AdmissionCaps(4, 1, (), 1, (("smithers", 4),))
        for value in (None, []):
            with self.subTest(value=value):
                raw = self.valid()
                raw["max_in_progress_per_profile"] = 1
                raw["dispatch_profiles"] = value
                with self.assertRaisesRegex(ConfigCompatibilityError, "dispatch_profiles must be a nonempty"):
                    ControllerConfig.from_mapping(raw, admission_caps=caps)

    def test_absent_admission_defaults_reach_compatibility_gate(self) -> None:
        defaults = AdmissionCaps(2, 1, (), 1, ())
        raw = self.valid()
        raw["max_in_progress"] = 2
        raw["max_in_progress_per_profile"] = 1
        cfg = ControllerConfig.from_mapping(raw, admission_caps=defaults)
        self.assertEqual(cfg.admission_caps, defaults)
        raw["max_in_progress"] = 4
        raw["max_in_progress_per_profile"] = 2
        with self.assertRaises(ConfigCompatibilityError):
            ControllerConfig.from_mapping(raw, admission_caps=defaults)

    def test_controller_telemetry_setting_is_not_accepted_in_hermes_config(self) -> None:
        raw = self.valid()
        raw["telemetry"] = {"linux_psi": {"some_avg10_warning": 10.0}}
        with self.assertRaises(ConfigCompatibilityError):
            ControllerConfig.from_mapping(raw, admission_caps=CAPS)

    def test_missing_or_unknown_setting_is_refused(self) -> None:
        raw = self.valid()
        del raw["failure_limit"]
        with self.assertRaisesRegex(ConfigCompatibilityError, "failure_limit"):
            ControllerConfig.from_mapping(raw, admission_caps=CAPS)
        raw = self.valid()
        raw["future_dispatch_behavior"] = True
        with self.assertRaisesRegex(ConfigCompatibilityError, "unsupported"):
            ControllerConfig.from_mapping(raw, admission_caps=CAPS)


class CliContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hermes = Path("/opt/hermes/bin/hermes")

    def test_dry_run_and_real_dispatch_use_supported_bounded_forms(self) -> None:
        self.assertEqual(
            build_dry_run_command(self.hermes, "team", 3, 2),
            [
                str(self.hermes), "kanban", "--board", "team", "dispatch",
                "--dry-run", "--max", "2", "--failure-limit", "3", "--json",
            ],
        )
        self.assertEqual(
            build_dispatch_command(self.hermes, "team", 3, 2),
            [
                str(self.hermes), "kanban", "--board", "team", "dispatch",
                "--max", "2", "--failure-limit", "3", "--json",
            ],
        )

    def test_decomposition_is_one_explicit_task_never_all(self) -> None:
        command = build_decompose_command(self.hermes, "team", "t_123")
        self.assertEqual(
            command,
            [
                str(self.hermes), "kanban", "--board", "team", "decompose",
                "t_123", "--author", "auto-decomposer", "--json",
            ],
        )
        self.assertNotIn("--all", command)

    def test_prediction_requires_exactly_one_spawned_task(self) -> None:
        payload = {
            "reclaimed": 0,
            "crashed": [],
            "timed_out": [],
            "stale": [],
            "auto_blocked": [],
            "promoted": 0,
            "spawned": [{"task_id": "t_1", "assignee": "reviewscope", "workspace": None}],
            "skipped_unassigned": [],
            "skipped_nonspawnable": [],
            "skipped_per_profile_capped": [],
            "auto_assigned_default": [],
            "reaped_terminal_workers": [], "respawn_guarded": [], "rate_limited": [], "skipped_locked": False, "memory_pressure": None,
        }
        parsed = parse_dispatch_prediction(json.dumps(payload), board="team", titles={"t_1": "Review"})
        self.assertEqual(parsed.task_id, "t_1")
        self.assertEqual(parsed.title, "Review")

        for spawned in ([], payload["spawned"] * 2):
            with self.subTest(spawned=spawned):
                bad = dict(payload)
                bad["spawned"] = spawned
                with self.assertRaises(CommandContractError):
                    parse_dispatch_prediction(json.dumps(bad), board="team", titles={"t_1": "Review"})

    def test_prediction_row_must_match_fenced_assignee_and_eligible_profiles(self) -> None:
        payload = {
            "reclaimed": 0, "crashed": [], "timed_out": [], "stale": [], "auto_blocked": [],
            "promoted": 0,
            "spawned": [{"task_id": "t_1", "assignee": "reviewscope", "workspace": None}],
            "skipped_unassigned": [], "skipped_nonspawnable": [], "skipped_per_profile_capped": [],
            "auto_assigned_default": [], "reaped_terminal_workers": [], "respawn_guarded": [],
            "rate_limited": [], "skipped_locked": False, "memory_pressure": None,
        }
        output = json.dumps(payload)
        titles = {"t_1": "Review"}
        parsed = parse_dispatch_prediction(
            output, board="team", titles=titles,
            assignees={"t_1": "reviewscope"}, eligible_profiles=("reviewscope",),
        )
        self.assertEqual(parsed.assignee, "reviewscope")
        with self.assertRaisesRegex(CommandContractError, "assignee differs"):
            parse_dispatch_prediction(
                output, board="team", titles=titles,
                assignees={"t_1": "builder"}, eligible_profiles=("reviewscope", "builder"),
            )
        with self.assertRaisesRegex(CommandContractError, "not dispatch-eligible"):
            parse_dispatch_prediction(
                output, board="team", titles=titles,
                assignees={"t_1": "reviewscope"}, eligible_profiles=("builder",),
            )

    def test_prediction_rejects_malformed_unknown_or_maintenance_output(self) -> None:
        with self.assertRaises(CommandContractError):
            parse_dispatch_prediction("not-json", board="team", titles={})
        with self.assertRaises(CommandContractError):
            parse_dispatch_prediction('{"spawned":[{"task_id":"t_x","assignee":"default"}]}', board="team", titles={})
        payload = {
            "reclaimed": 1,
            "crashed": [], "timed_out": [], "stale": [], "auto_blocked": [],
            "promoted": 0,
            "spawned": [{"task_id": "t_1", "assignee": "default", "workspace": None}],
            "skipped_unassigned": [], "skipped_nonspawnable": [],
            "skipped_per_profile_capped": [], "auto_assigned_default": [],
            "reaped_terminal_workers": [], "respawn_guarded": [], "rate_limited": [], "skipped_locked": False, "memory_pressure": None,
        }
        with self.assertRaisesRegex(CommandContractError, "maintenance"):
            parse_dispatch_prediction(json.dumps(payload), board="team", titles={"t_1": "Build"})


if __name__ == "__main__":
    unittest.main()
