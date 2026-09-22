"""Native macOS sensors: paging counters, not historical swap occupancy."""
import os
import subprocess
import sys
import time

import psutil
from .policy import Sample


def sample_mac():
    if sys.platform != 'darwin':
        raise ValueError('only native Darwin telemetry is supported')
    raw = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level'],
                         capture_output=True, text=True, timeout=3, check=True)
    pressure = int(raw.stdout.strip())
    swap = psutil.swap_memory()
    sample = Sample(time.monotonic(), os.cpu_count(), os.getloadavg()[0],
                    psutil.virtual_memory().available, pressure, swap.sin, swap.sout)
    if not sample.valid():
        raise ValueError('invalid native telemetry')
    return sample
