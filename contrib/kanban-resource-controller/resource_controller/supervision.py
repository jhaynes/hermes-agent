from __future__ import annotations

from dataclasses import dataclass
import subprocess
import threading
from typing import Mapping, Sequence


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool

    @property
    def uncertain(self) -> bool:
        return self.timed_out or self.truncated or self.returncode != 0


def run_supervised(
    argv: Sequence[str],
    *,
    observe_timeout: float,
    max_output: int,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Observe a deadline, then drain naturally with bounded retained output."""
    if not argv or observe_timeout <= 0 or max_output <= 0:
        raise ValueError("invalid command supervision arguments")
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
        close_fds=True,
    )
    assert process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    budget = max_output
    truncated = False
    guard = threading.Lock()

    def drain(name: str, pipe) -> None:
        nonlocal budget, truncated
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            with guard:
                keep = min(len(chunk), budget)
                if keep:
                    buffers[name].extend(chunk[:keep])
                    budget -= keep
                if keep != len(chunk):
                    truncated = True
        pipe.close()

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=observe_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # A deadline is observation only: send no signal and retain singleton
        # authority while the finite command drains and exits naturally.
        process.wait()
    for thread in threads:
        thread.join()
    return CommandResult(
        tuple(argv),
        process.returncode,
        bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
        timed_out,
        truncated,
    )
