from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "contract_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("contract_gate_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(module, env: dict, git: subprocess.CompletedProcess | None = None, result=None):
    err, out = io.StringIO(), io.StringIO()
    patches = [unittest.mock.patch.dict(os.environ, env, clear=True)]
    if git is not None:
        patches.append(unittest.mock.patch.object(module.subprocess, "run", return_value=git))
    if result is not None:
        patches.append(unittest.mock.patch.object(module.unittest.TextTestRunner, "run", return_value=result))
        patches.append(unittest.mock.patch.object(module.unittest.TestLoader, "loadTestsFromName", return_value=unittest.TestSuite()))
    for p in patches:
        p.start()
    try:
        with redirect_stderr(err), redirect_stdout(out):
            code = module.main()
    finally:
        for p in reversed(patches):
            p.stop()
    return code, err.getvalue() + out.getvalue()


def _git(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr="boom" if returncode else "")


def _result(*, run: int, skipped: int = 0, ok: bool = True):
    fake = unittest.mock.Mock()
    fake.testsRun = run
    fake.skipped = [("t", "why")] * skipped
    fake.wasSuccessful.return_value = ok
    return fake


class ContractGateScriptTests(unittest.TestCase):
    """The release gate's own control logic (it is mandatory, never a skip)."""

    def setUp(self) -> None:
        self.module = _load()
        self.pinned = self.module.EXPECTED_HERMES_COMMIT

    def test_pinned_commit_is_the_audited_source(self) -> None:
        self.assertEqual(self.pinned, "0e0a29ad315da6b6fd5b63e2903600af85e839e5")

    def test_missing_env_fails_hard(self) -> None:
        code, text = _run(self.module, {})
        self.assertEqual(code, 1)
        self.assertIn("HERMES_SOURCE_ROOT is required", text)

    def test_unreadable_head_fails(self) -> None:
        code, text = _run(self.module, {"HERMES_SOURCE_ROOT": "/x"}, git=_git("", returncode=128))
        self.assertEqual(code, 1)
        self.assertIn("cannot read HEAD", text)

    def test_wrong_head_fails_before_running_tests(self) -> None:
        with unittest.mock.patch.object(self.module.unittest.TextTestRunner, "run") as runner:
            code, text = _run(self.module, {"HERMES_SOURCE_ROOT": "/x"}, git=_git("deadbeef\n"))
            runner.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("expected " + self.pinned, text)

    def test_skipped_gate_tests_fail_the_gate(self) -> None:
        code, text = _run(self.module, {"HERMES_SOURCE_ROOT": "/x"}, git=_git(self.pinned + "\n"),
                          result=_result(run=2, skipped=1))
        self.assertEqual(code, 1)
        self.assertIn("skipped", text)

    def test_failed_or_empty_gate_fails(self) -> None:
        code, _ = _run(self.module, {"HERMES_SOURCE_ROOT": "/x"}, git=_git(self.pinned + "\n"),
                       result=_result(run=2, ok=False))
        self.assertEqual(code, 1)
        code, _ = _run(self.module, {"HERMES_SOURCE_ROOT": "/x"}, git=_git(self.pinned + "\n"),
                       result=_result(run=0))
        self.assertEqual(code, 1)

    def test_pinned_head_and_passing_tests_pass(self) -> None:
        code, text = _run(self.module, {"HERMES_SOURCE_ROOT": "/x"}, git=_git(self.pinned + "\n"),
                          result=_result(run=2))
        self.assertEqual(code, 0)
        self.assertIn("CONTRACT GATE PASSED", text)


if __name__ == "__main__":
    unittest.main()
