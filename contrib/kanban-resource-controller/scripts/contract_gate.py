#!/usr/bin/env python3
"""Release gate: proves the controller's worker fingerprint contract matches
the PINNED Hermes source, byte for byte, for a live process.

Unlike the ordinary hermetic test run (which skips
tests/test_contract_gate.py when HERMES_SOURCE_ROOT is unset), this script is
MANDATORY: it FAILS (nonzero exit), not skips, if the pinned source or the
right commit is unavailable. See RUNBOOK.md "Identity contract" for when this
must run (a required pre-install step) and plan item 7.

Usage:
    HERMES_SOURCE_ROOT=/Users/jhaynes/.hermes/hermes-agent \
        python3 scripts/contract_gate.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

EXPECTED_HERMES_COMMIT = "0e0a29ad315da6b6fd5b63e2903600af85e839e5"


def main() -> int:
    root = os.environ.get("HERMES_SOURCE_ROOT")
    if not root:
        print(
            "CONTRACT GATE FAILED: HERMES_SOURCE_ROOT is required and unset. "
            "This is a mandatory pre-install gate, not an optional check.",
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"CONTRACT GATE FAILED: cannot read HEAD of {root}: {result.stderr}", file=sys.stderr)
        return 1
    commit = result.stdout.strip()
    if commit != EXPECTED_HERMES_COMMIT:
        print(
            f"CONTRACT GATE FAILED: pinned Hermes source at {root} is {commit}, "
            f"expected {EXPECTED_HERMES_COMMIT}",
            file=sys.stderr,
        )
        return 1

    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, package_root)
    sys.path.insert(0, os.path.join(package_root, "tests"))

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("test_contract_gate")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.skipped:
        print(
            f"CONTRACT GATE FAILED: {len(result.skipped)} test(s) skipped "
            "even though HERMES_SOURCE_ROOT is set — the gate must never "
            "silently pass via a skip.",
            file=sys.stderr,
        )
        return 1
    if not result.wasSuccessful():
        print("CONTRACT GATE FAILED: see failures above.", file=sys.stderr)
        return 1
    if result.testsRun == 0:
        print("CONTRACT GATE FAILED: no tests ran.", file=sys.stderr)
        return 1
    print(f"CONTRACT GATE PASSED: {result.testsRun} test(s) against pinned Hermes {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
