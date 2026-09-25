#!/usr/bin/env python3
"""Mandatory release gate for the exact 9b0b4f26 rollback reader."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


EXPECTED_ARCHIVE_NAME = "kanban-resource-controller-9b0b4f261e2b495e53c2012d074b5cebb9445ab4.tar.gz"


def main() -> int:
    archive = os.environ.get("CONTROLLER_OLD_ARCHIVE")
    if not archive:
        print("OLD-READER GATE FAILED: CONTROLLER_OLD_ARCHIVE is required", file=sys.stderr)
        return 1
    if Path(archive).name != EXPECTED_ARCHIVE_NAME:
        print(f"OLD-READER GATE FAILED: expected {EXPECTED_ARCHIVE_NAME}", file=sys.stderr)
        return 1
    package_root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(package_root), str(package_root / "tests")]
    suite = unittest.TestLoader().loadTestsFromName("test_old_reader_compat")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped or not result.wasSuccessful() or result.testsRun != 1:
        print("OLD-READER GATE FAILED: test must run once without skip", file=sys.stderr)
        return 1
    print("OLD-READER GATE PASSED: 9b0b4f26 rejects alerting and reads additive journal/status fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
