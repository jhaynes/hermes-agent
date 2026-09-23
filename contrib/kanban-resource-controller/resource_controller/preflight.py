from __future__ import annotations

from pathlib import Path
import os
import re
import stat
from typing import Callable, Mapping

from .supervision import CommandResult, run_supervised


class PreflightError(RuntimeError):
    pass


Runner = Callable[[list[str], float, int], CommandResult]
_SHA = re.compile(r"^[0-9a-f]{40}$")


def read_kanban_config(path: Path) -> Mapping[str, object]:
    if path.is_symlink():
        raise PreflightError("config.yaml must not be a symlink")
    try:
        info = path.stat()
    except OSError as exc:
        raise PreflightError(f"config.yaml unreadable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1024 * 1024:
        raise PreflightError("config.yaml must be an owned, bounded regular file")
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PreflightError(f"config.yaml parse failed: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("kanban"), dict):
        raise PreflightError("config.yaml has no kanban mapping")
    return payload["kanban"]


def _default_runner(argv: list[str], timeout: float, limit: int) -> CommandResult:
    return run_supervised(argv, observe_timeout=timeout, max_output=limit)


def verify_source_baseline(source_root: Path, expected: str, *, runner: Runner = _default_runner) -> None:
    if not source_root.is_absolute() or not _SHA.fullmatch(expected):
        raise PreflightError("invalid source baseline contract")
    result = runner(
        ["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"],
        15.0,
        4096,
    )
    actual = result.stdout.strip()
    if result.uncertain or actual != expected:
        raise PreflightError(f"source baseline mismatch: expected {expected}, observed {actual or 'unknown'}")
