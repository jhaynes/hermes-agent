"""Execution slots shared across pool generations, scoped to the owning profile."""
from contextlib import contextmanager
import threading

from hermes_constants import hermes_home_key


class LoadAdmission:
    """Track actual running work, including work started before the gate was enabled.

    Existing accepted batches may wait in-process; this is not a durable queue.
    Each waiter re-resolves policy, so cap shrink, recovery and rollback apply
    without replacing executors or cancelling running work.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._active = {}

    def at_capacity(self, status):
        with self._condition:
            return status.pressured and self._active.get(hermes_home_key(), 0) >= status.effective_cap

    @contextmanager
    def slot(self, status_fn):
        key = hermes_home_key()
        with self._condition:
            while True:
                status = status_fn()
                if not status.pressured or self._active.get(key, 0) < status.effective_cap:
                    break
                self._condition.wait(.5)
            self._active[key] = self._active.get(key, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                self._active[key] -= 1
                if not self._active[key]:
                    del self._active[key]
                self._condition.notify_all()
