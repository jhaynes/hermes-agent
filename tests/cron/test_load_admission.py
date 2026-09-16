"""Real scheduler pools and fire-claim boundary, with fake bounded job bodies."""
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cron import scheduler as sched
from gateway import system_load as load
from hermes_constants import get_hermes_home


@pytest.fixture
def setup_load(monkeypatch):
    get_hermes_home().joinpath('config.yaml').write_text('system_load:\n  enabled: true\n  dwell_seconds: 0\n')
    sample = [load.LoadSample(100, 12., 10, 0)]
    monkeypatch.setattr(load, 'sample_system_load', lambda: sample[0])
    monkeypatch.setattr(sched, '_maybe_run_worktree_maintenance', lambda: None)
    monkeypatch.setattr(sched, '_sweep_mcp_orphans', lambda: None)
    monkeypatch.setattr(sched, '_should_yield_tick_to_fresh_gateway', lambda: None)
    monkeypatch.setattr(sched, 'claim_job_for_fire', lambda *args, **kwargs: True)
    sched._shutdown_parallel_pool()
    yield sample
    sched._shutdown_parallel_pool()
    for job_id in list(sched._running_job_ids):
        sched.release_running_job(job_id)


def jobs(prefix, n):
    return [dict(id=f'{prefix}-{i}', name=f'{prefix}-{i}', prompt='test', schedule='every 5m',
                 enabled=True, next_run_at='2020-01-01T00:00:00', deliver='local') for i in range(n)]


@pytest.mark.parametrize('base,load1,cap', [(None, 12., 2), (None, 21., 1), (4, 12., 2), (1, 12., 1), (4, None, 4), (4, 0., 4)])
def test_tick_limits_actual_jobs_and_finishes_all_queued(setup_load, monkeypatch, base, load1, cap):
    setup_load[0] = load.LoadSample(100, load1, 10, 0)
    monkeypatch.setattr(sched, '_resolve_max_parallel_workers', lambda: base)
    monkeypatch.setattr(sched, 'get_due_jobs', lambda: jobs('bounded', 4))
    active, peak = 0, 0
    completed = []
    lock = threading.Lock()
    entered, overflow, release = threading.Event(), threading.Event(), threading.Event()

    def run(job, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == cap:
                entered.set()
            if active > cap:
                overflow.set()
        try:
            assert release.wait(6)
            completed.append(job['id'])
            return True
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sched, 'run_one_job', run)
    with ThreadPoolExecutor(1) as parent:
        future = parent.submit(contextvars.copy_context().run, sched.tick, verbose=False)
        try:
            assert entered.wait(5)
            overflow.wait(2)
        finally:
            release.set()
        assert future.result(5) == 4
    assert len(set(completed)) == 4
    assert peak <= cap


def test_shrink_across_pool_generations_waits_before_fire_claim_and_recovers(setup_load, monkeypatch):
    from cron import scheduler_load
    from concurrent.futures import wait

    base = [4]
    monkeypatch.setattr(sched, '_resolve_max_parallel_workers', lambda: base[0])
    due = [jobs('old', 2)]
    monkeypatch.setattr(sched, 'get_due_jobs', lambda: due[0])
    entered = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    blocked, started_new = threading.Event(), threading.Event()
    claims = []
    completed = []
    monkeypatch.setattr(sched, 'claim_job_for_fire', lambda job_id, **kw: claims.append(job_id) or True)
    original_status = scheduler_load.cron_load_status

    def status():
        result = original_status()
        if result.level == 'critical':
            blocked.set()
        return result

    monkeypatch.setattr(scheduler_load, 'cron_load_status', status)

    def run(job, **kwargs):
        if job['id'].startswith('old'):
            i = int(job['id'].split('-')[1])
            entered[i].set()
            assert releases[i].wait(10)
        else:
            started_new.set()
        completed.append(job['id'])
        return True

    monkeypatch.setattr(sched, 'run_one_job', run)
    futures = []
    try:
        assert sched.tick(verbose=False, sync=False) == 2
        assert all(event.wait(5) for event in entered)
        futures.extend(sched._running_futures.values())
        setup_load[0] = load.LoadSample(200, 21., 10, 0)
        base[0] = 3  # creates another executor while the old one is still alive
        due[0] = jobs('new', 2)
        assert sched.tick(verbose=False, sync=False) == 2
        futures.extend(sched._running_futures.values())
        assert blocked.wait(5)
        started_new.wait(2)
        assert set(claims) == {'old-0', 'old-1'}
        releases[0].set()
        futures[0].result(5)
        started_new.wait(2)
        assert not started_new.is_set()  # one old job still fills the critical lane
        setup_load[0] = load.LoadSample(400, 1., 10, 0)
        assert started_new.wait(5)  # recovers without cancelling the old job
    finally:
        for event in releases:
            event.set()
        wait(futures, timeout=5)
    assert set(completed) == {'old-0', 'old-1', 'new-0', 'new-1'}
