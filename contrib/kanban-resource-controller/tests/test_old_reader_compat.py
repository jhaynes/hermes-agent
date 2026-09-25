from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


_ARCHIVE_ENV = "CONTROLLER_OLD_ARCHIVE"
_EXPECTED_SHA256 = "5709b8ccd7a6125ec2c52c76711a6553afef65344d26e4f18feb672885265bef"


@unittest.skipUnless(os.environ.get(_ARCHIVE_ENV), f"{_ARCHIVE_ENV} not set; mandatory old-reader gate runs separately")
class OldReaderCompatibilityTests(unittest.TestCase):
    def test_9b0b4f26_rejects_alerting_and_ignores_additive_fields(self) -> None:
        archive = Path(os.environ[_ARCHIVE_ENV])
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), _EXPECTED_SHA256)
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            extracted = base / "extracted"
            extracted.mkdir()
            with tarfile.open(archive, "r:gz") as package:
                package.extractall(extracted, filter="data")
            package_root = next(extracted.glob("*/contrib/kanban-resource-controller"))
            config = {
                "hermes_executable": str(base / "hermes"),
                "hermes_home": str(base / "home"),
                "source_root": str(base / "source"),
                "expected_source_commit": "a" * 40,
                "dispatcher_lock": str(base / "lock"),
                "state_dir": str(base / "state"),
                "boards": {"default": str(base / "board.db")},
                "interval_seconds": 30,
            }
            plain = base / "plain.json"
            with_alerting = base / "alerting.json"
            plain.write_text(json.dumps(config), encoding="utf-8")
            with_alerting.write_text(json.dumps({**config, "alerting": {
                "stuck_after_seconds": 180,
                "notify_timeout_seconds": 10,
            }}), encoding="utf-8")
            plain.chmod(0o600)
            with_alerting.chmod(0o600)
            probe = base / "probe.py"
            probe.write_text(
                """from pathlib import Path
import sys
from resource_controller.spec import RuntimeSpec, SpecError
from resource_controller.storage import SecureStateStore
plain, alerting, state = map(Path, sys.argv[1:])
RuntimeSpec.read(plain)
try:
    RuntimeSpec.read(alerting)
except SpecError:
    pass
else:
    raise SystemExit('old RuntimeSpec accepted alerting')
store = SecureStateStore(state)
store.write_json('pending.json', {'outcome': 'pending', 'error': {'type': 'X', 'message': 'Y'}})
assert store.has_pending_uncertainty() is True
store.write_json('pending.json', {'outcome': 'reconciled', 'error': {'type': 'X', 'message': 'Y'}})
assert store.has_pending_uncertainty() is False
store.write_json('status.json', {'schema_version': 2, 'reason': 'uncertain-outcome', 'error_type': 'X', 'error': 'Y'})
assert store.read_json('status.json')['error_type'] == 'X'
""",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(package_root)
            completed = subprocess.run(
                [sys.executable, str(probe), str(plain), str(with_alerting), str(base / "old-state")],
                cwd=package_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
