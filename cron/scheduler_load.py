"""Load admission before cron fire claims, shared across executor generations."""
from gateway.load_admission import LoadAdmission
from gateway.system_load import load_status

_job_admission = LoadAdmission()


def cron_admission(base, level, policy):
    if not policy.enabled or level not in ('elevated', 'critical'):
        return base
    if base is None:
        return (policy.unbounded_critical_cap if level == 'critical'
                else policy.unbounded_elevated_cap)
    return min(base, 1 if level == 'critical' else max(1, base // policy.elevated_cap_divisor))


def cron_load_status():
    from cron.scheduler import _resolve_max_parallel_workers
    return load_status('cron', _resolve_max_parallel_workers(), cron_admission)


def run_admitted(process_job, job):
    # Track even disabled work so enabling/shrinking cannot bypass older pools.
    # Wait before claiming: queued one-shot leases must not expire while waiting.
    with _job_admission.slot(cron_load_status):
        return process_job(job)
