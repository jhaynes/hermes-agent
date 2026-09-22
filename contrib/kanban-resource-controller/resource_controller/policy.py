"""Admission-only resource policy. No scheduler, process or wall-clock state."""
from dataclasses import dataclass
import math

GIB = 2**30


@dataclass(frozen=True)
class Sample:
    at: float
    cores: int
    load: float
    available: int
    pressure: int
    sin: int
    sout: int

    def valid(self):
        values = (self.at, self.cores, self.load, self.available, self.pressure, self.sin, self.sout)
        return (all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values)
                and type(self.cores) is int and self.cores > 0 and self.pressure in (1, 2, 4))


class Gate:
    def __init__(self):
        self.reset()

    def reset(self):
        self.previous = None
        self.healthy_since = None

    def observe(self, sample):
        if sample is None or not sample.valid():
            self.reset()
            return 'resource:unknown'
        old, self.previous = self.previous, sample
        reason = None
        if old is None:
            reason = 'cold'
        elif sample.at <= old.at or sample.at - old.at > 60:
            reason = 'gap'
        elif sample.sin < old.sin or sample.sout < old.sout:
            reason = 'counter-reset'
        elif sample.sin > old.sin:
            reason = 'paging-in'
        elif sample.sout > old.sout:
            reason = 'paging-out'
        elif sample.pressure != 1:
            reason = 'pressure'
        elif sample.load >= sample.cores:
            reason = 'load'
        elif sample.available < 4 * GIB:
            reason = 'memory'
        elif sample.load > .8 * sample.cores or sample.available < 5 * GIB:
            reason = 'recovery-band'
        if reason:
            self.healthy_since = None
            return 'resource:' + reason
        if self.healthy_since is None:
            self.healthy_since = sample.at
        return 'eligible' if sample.at - self.healthy_since >= 120 else 'resource:dwell'
