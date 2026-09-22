"""Private durable controller state; never writes Hermes configuration or ESTOP."""
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def check_private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'unsafe private artifact: {path}')


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_private(self.root, directory=True)

    def path(self, name):
        if Path(name).name != name:
            raise ValueError('state name must be a basename')
        path = self.root / name
        if path.exists() or path.is_symlink():
            check_private(path)
        return path

    def read(self, name):
        path = self.path(name)
        try:
            with path.open() as stream:
                return json.load(stream)
        except FileNotFoundError:
            return None

    def sync(self):
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write(self, name, value):
        path = self.path(name)
        fd, temporary = tempfile.mkstemp(prefix='.write-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self.sync()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, name):
        self.path(name).unlink(missing_ok=True)
        self.sync()

    def log(self, message):
        path = self.path('events.log')
        data = (json.dumps({'at': time.time(), 'message': message[:2048]}) + '\n').encode()
        if path.exists() and path.stat().st_size + len(data) > 65536:
            oldest = self.path('events.log.2')
            oldest.unlink(missing_ok=True)
            previous = self.path('events.log.1')
            if previous.exists():
                previous.replace(oldest)
            path.replace(previous)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(data)


class Singleton:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        # Do not unlink this file: replacing its inode would split authority.
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.set_inheritable(self.fd, False)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_):
        os.close(self.fd)
        self.fd = None
