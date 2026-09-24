from __future__ import annotations

import unittest
import unittest.mock

from resource_controller.worker_identity import (
    IdentityConflict,
    IdentityHold,
    IdentityMalformed,
    IdentityMismatch,
    IdentityUnavailable,
    IdentityUnsupportedPlatform,
    IdentityUnverified,
    StoredFingerprint,
    current_fingerprint,
    current_instantiation_epoch,
    current_process_start,
    matches,
    parse_stored_fingerprint,
    require_consistent,
    require_match,
)


GOLDEN_COMPOSITE = "|179027411681"          # this host's real live row
GOLDEN_CREATE_TIME = 1790274116.812699      # psutil create_time() for that row


class ParseStoredFingerprintTests(unittest.TestCase):
    def test_golden_live_row_parses_as_composite(self) -> None:
        parsed = parse_stored_fingerprint(GOLDEN_COMPOSITE)
        self.assertEqual(parsed, StoredFingerprint(epoch="", start=179027411681, raw=GOLDEN_COMPOSITE))

    def test_linux_shaped_composite_parses(self) -> None:
        value = "b3d1e2:120345|987654321"
        parsed = parse_stored_fingerprint(value)
        self.assertEqual(parsed.epoch, "b3d1e2:120345")
        self.assertEqual(parsed.start, 987654321)

    def test_unverified_marker_raises_identity_unverified(self) -> None:
        with self.assertRaises(IdentityUnverified):
            parse_stored_fingerprint("unverified")

    def test_null_raises_identity_malformed_not_generic_valueerror(self) -> None:
        with self.assertRaises(IdentityMalformed) as ctx:
            parse_stored_fingerprint(None)
        self.assertNotEqual(type(ctx.exception), ValueError)

    def test_legacy_integer_is_unsupported(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint(1000)

    def test_bool_is_rejected_even_though_bool_is_an_int_subclass(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint(True)

    def test_double_pipe_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("a|b|1")

    def test_zero_start_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("|0")

    def test_negative_start_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("|-5")

    def test_scientific_notation_start_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("|1e3")

    def test_leading_zero_start_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("|0179027411681")

    def test_oversized_string_rejected_before_regex(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("x" * 10_000 + "|1")

    def test_empty_string_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("")

    def test_no_pipe_rejected(self) -> None:
        with self.assertRaises(IdentityMalformed):
            parse_stored_fingerprint("179027411681")


class EpochTests(unittest.TestCase):
    def _fake_reader(self, boot_id: str | None, pid1_stat: str | None):
        def reader(path):
            name = str(path)
            if name.endswith("boot_id"):
                if boot_id is None:
                    raise OSError("no boot id")
                return boot_id
            if name.endswith("1/stat"):
                if pid1_stat is None:
                    raise OSError("no pid1 stat")
                return pid1_stat
            raise AssertionError(path)
        return reader

    def test_both_present(self) -> None:
        # /proc/1/stat: "1 (systemd) S <fields 4..21 = 18 values> 120345 ..."
        # after rsplit(")",1)[1].split(): index0=state 'S', field22(1-indexed)=index19
        fields = ["S"] + [str(i) for i in range(18)] + ["120345"] + ["0"] * 20
        stat = "1 (systemd) " + " ".join(fields)
        epoch = current_instantiation_epoch(read_text=self._fake_reader("b3d1e2\n", stat))
        self.assertEqual(epoch, "b3d1e2:120345")

    def test_boot_id_only(self) -> None:
        epoch = current_instantiation_epoch(read_text=self._fake_reader("b3d1e2\n", None))
        self.assertEqual(epoch, "b3d1e2:")

    def test_pid1_only(self) -> None:
        fields = ["S"] + [str(i) for i in range(18)] + ["120345"] + ["0"] * 20
        stat = "1 (systemd) " + " ".join(fields)
        epoch = current_instantiation_epoch(read_text=self._fake_reader(None, stat))
        self.assertEqual(epoch, ":120345")

    def test_neither_present_gives_empty_string(self) -> None:
        epoch = current_instantiation_epoch(read_text=self._fake_reader(None, None))
        self.assertEqual(epoch, "")

    def test_comm_containing_spaces_and_parens_is_handled_via_rsplit(self) -> None:
        fields = ["S"] + [str(i) for i in range(18)] + ["999"] + ["0"] * 20
        stat = "1 (my ) proc) " + " ".join(fields)
        epoch = current_instantiation_epoch(read_text=self._fake_reader(None, stat))
        self.assertEqual(epoch, ":999")


class CurrentProcessStartTests(unittest.TestCase):
    def test_darwin_uses_rounded_centiseconds(self) -> None:
        with unittest.mock.patch("sys.platform", "darwin"):
            start = current_process_start(123, create_time_fn=lambda pid: GOLDEN_CREATE_TIME)
        self.assertEqual(start, 179027411681)

    def test_darwin_read_failure_gives_none(self) -> None:
        def boom(pid):
            raise Exception("no such process")

        with unittest.mock.patch("sys.platform", "darwin"):
            start = current_process_start(123, create_time_fn=boom)
        self.assertIsNone(start)

    def test_linux_uses_proc_stat_field_22_via_split_index_21(self) -> None:
        # .split()[21] is the port of the exact (buggy-if-comm-has-spaces) upstream
        # quirk; use a plain comm here so the field offset itself is exercised.
        fields = ["1", "(comm)", "S"] + [str(i) for i in range(4, 22)] + ["42424242"] + ["0"] * 20
        stat = " ".join(fields)
        def reader(path):
            return stat
        with unittest.mock.patch("sys.platform", "linux"):
            start = current_process_start(1, read_text=reader)
        self.assertEqual(start, 42424242)

    def test_linux_read_failure_gives_none(self) -> None:
        def reader(path):
            raise OSError("gone")
        with unittest.mock.patch("sys.platform", "linux"):
            start = current_process_start(1, read_text=reader)
        self.assertIsNone(start)

    def test_unsupported_platform_raises(self) -> None:
        with unittest.mock.patch("sys.platform", "win32"):
            with self.assertRaises(IdentityUnsupportedPlatform):
                current_process_start(1)


class CurrentFingerprintTests(unittest.TestCase):
    def test_darwin_golden_value_matches_pinned_hermes_output(self) -> None:
        with unittest.mock.patch("sys.platform", "darwin"):
            fp = current_fingerprint(12936, epoch="", create_time_fn=lambda pid: GOLDEN_CREATE_TIME)
        self.assertEqual(fp, GOLDEN_COMPOSITE)

    def test_unavailable_start_raises_identity_unavailable(self) -> None:
        with unittest.mock.patch("sys.platform", "darwin"):
            with self.assertRaises(IdentityUnavailable):
                current_fingerprint(1, epoch="", create_time_fn=lambda pid: (_ for _ in ()).throw(Exception()))


class MatchesTests(unittest.TestCase):
    def test_exact_match_true(self) -> None:
        stored = parse_stored_fingerprint(GOLDEN_COMPOSITE)
        self.assertTrue(matches(stored, GOLDEN_COMPOSITE))

    def test_start_off_by_one_cs_is_a_mismatch_no_tolerance(self) -> None:
        stored = parse_stored_fingerprint("|179027411681")
        self.assertFalse(matches(stored, "|179027411682"))

    def test_epoch_mismatch_is_a_mismatch(self) -> None:
        stored = parse_stored_fingerprint("a|100")
        self.assertFalse(matches(stored, "b|100"))

    def test_require_match_raises_identity_mismatch(self) -> None:
        with self.assertRaises(IdentityMismatch):
            require_match("|179027411681", "|179027411682", context="t_x")

    def test_require_match_passes_on_agreement(self) -> None:
        require_match(GOLDEN_COMPOSITE, GOLDEN_COMPOSITE, context="t_x")


class RequireConsistentTests(unittest.TestCase):
    def test_tasks_and_runs_agreeing_returns_parsed(self) -> None:
        parsed = require_consistent(GOLDEN_COMPOSITE, 12936, GOLDEN_COMPOSITE, 12936, context="t_c9f3e704")
        self.assertEqual(parsed.raw, GOLDEN_COMPOSITE)

    def test_disagreeing_fingerprints_raise_identity_conflict(self) -> None:
        with self.assertRaises(IdentityConflict):
            require_consistent(GOLDEN_COMPOSITE, 12936, "|179027411682", 12936, context="t_c9f3e704")

    def test_disagreeing_pids_raise_identity_conflict(self) -> None:
        with self.assertRaises(IdentityConflict):
            require_consistent(GOLDEN_COMPOSITE, 12936, GOLDEN_COMPOSITE, 12937, context="t_c9f3e704")


if __name__ == "__main__":
    unittest.main()
