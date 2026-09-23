from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


class StorageError(RuntimeError):
    pass


class SecureStateStore:
    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise StorageError("state directory must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise StorageError("state directory must be an owned directory")
        os.chmod(root, 0o700)
        self.root = root

    def _path(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise StorageError("state name must be a plain filename")
        return self.root / name

    def write_json(self, name: str, value: Any) -> None:
        destination = self._path(name)
        if destination.is_symlink():
            raise StorageError("state file must not be a symlink")
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def read_json(self, name: str) -> Any:
        path = self._path(name)
        if path.is_symlink():
            raise StorageError("state file must not be a symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StorageError("state file must be owned and regular")
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def has_pending_uncertainty(self) -> bool:
        path = self._path("pending.json")
        if not path.exists():
            return False
        payload = self.read_json("pending.json")
        return not isinstance(payload, dict) or payload.get("outcome") != "reconciled"
