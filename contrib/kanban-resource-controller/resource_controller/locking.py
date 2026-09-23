from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path


class LockContended(RuntimeError):
    pass


class SingletonLock:
    """Lifetime flock owner that never unlinks the shared lock inode."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("lock already acquired")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockContended(str(self.path)) from exc
            raise
        self._fd = fd

    def fileno(self) -> int:
        if self._fd is None:
            raise RuntimeError("lock not acquired")
        return self._fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def __enter__(self) -> "SingletonLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
