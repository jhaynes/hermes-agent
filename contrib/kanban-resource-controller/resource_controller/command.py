"""Bounded CLI supervision without killing or abandoning uncertain work."""
import json
import os
import subprocess
import time

import psutil


class Uncertain(RuntimeError):
    pass


class Command:
    def __init__(self, store):
        self.store = store
        self.process = None
        self.output = bytearray()
        self.overflow = False

    def start(self, argv, env, cwd, context):
        if self.store.read('pending.json') is not None:
            raise Uncertain('previous command requires operator reconciliation')
        pending = {**context, 'argv': argv, 'at': time.time(), 'pid': None,
                   'created': None, 'uncertain': True}
        # Crash between journal and spawn is deliberately an operator hold too.
        self.store.write('pending.json', pending)
        self.output = bytearray()
        self.overflow = False
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        close_fds=True)
        pending.update(pid=self.process.pid, created=psutil.Process(self.process.pid).create_time())
        self.store.write('pending.json', pending)
        os.set_blocking(self.process.stdout.fileno(), False)

    def drain(self):
        if self.process is None or self.process.stdout.closed:
            return
        # Bounded work per tick even if a broken CLI produces infinite output.
        for _ in range(16):
            try:
                chunk = os.read(self.process.stdout.fileno(), 8192)
            except BlockingIOError:
                break
            if not chunk:
                self.process.stdout.close()
                break
            remaining = 65536 - len(self.output)
            self.output.extend(chunk[:remaining])
            self.overflow |= len(chunk) > remaining
        self.process.poll()  # reap our child; never signal it or its workers

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.drain()
            if self.overflow:
                raise Uncertain('command output exceeded 64KiB')
            if self.process.poll() is not None and self.process.stdout.closed:
                if self.process.returncode != 0:
                    raise Uncertain(f'command exit {self.process.returncode}')
                try:
                    return json.loads(self.output)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise Uncertain('command output is not one JSON value') from exc
            if time.monotonic() >= deadline:
                raise Uncertain('command timeout; still supervised, never retried')
            time.sleep(.02)
