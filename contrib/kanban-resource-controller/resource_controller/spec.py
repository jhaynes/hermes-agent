from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping


class SpecError(RuntimeError):
    pass


_KEYS = {
    "hermes_executable", "hermes_home", "source_root", "expected_source_commit",
    "dispatcher_lock", "state_dir", "boards", "interval_seconds",
}
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RuntimeSpec:
    hermes_executable: Path
    hermes_home: Path
    source_root: Path
    expected_source_commit: str
    dispatcher_lock: Path
    state_dir: Path
    boards: Mapping[str, Path]
    interval_seconds: int

    @classmethod
    def read(cls, path: Path) -> "RuntimeSpec":
        if path.is_symlink():
            raise SpecError("runtime spec must not be a symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SpecError("runtime spec must be an owned regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SpecError("runtime spec mode must be 0600")
        if info.st_size > 1024 * 1024:
            raise SpecError("runtime spec is oversized")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpecError(f"runtime spec unreadable: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != _KEYS:
            raise SpecError("runtime spec keys do not match the pinned contract")
        paths = {
            key: _absolute(raw[key], key)
            for key in (
                "hermes_executable", "hermes_home", "source_root",
                "dispatcher_lock", "state_dir",
            )
        }
        commit = raw["expected_source_commit"]
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise SpecError("expected_source_commit must be a 40-character lowercase SHA")
        interval = raw["interval_seconds"]
        if not isinstance(interval, int) or isinstance(interval, bool) or interval != 30:
            raise SpecError("interval_seconds must be exactly 30")
        boards_raw = raw["boards"]
        if not isinstance(boards_raw, dict) or not boards_raw:
            raise SpecError("boards must be a nonempty object")
        boards: dict[str, Path] = {}
        for slug, value in boards_raw.items():
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                raise SpecError("board slug is invalid")
            boards[slug] = _absolute(value, f"boards.{slug}")
        try:
            paths["state_dir"].relative_to(paths["source_root"])
        except ValueError:
            pass
        else:
            raise SpecError("state_dir must be outside installed source")
        return cls(
            paths["hermes_executable"],
            paths["hermes_home"],
            paths["source_root"],
            commit,
            paths["dispatcher_lock"],
            paths["state_dir"],
            boards,
            interval,
        )


def _absolute(value: object, key: str) -> Path:
    if not isinstance(value, str):
        raise SpecError(f"{key} must be a path string")
    path = Path(value)
    if not path.is_absolute():
        raise SpecError(f"{key} must be absolute")
    return path
