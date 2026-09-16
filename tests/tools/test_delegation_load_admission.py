"""Exercise real batch runners with bounded fake children, not executor handles."""
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

from gateway import system_load as load
from tools import delegate_tool as dt
from tools import delegate_tool_dispatch as dispatch


@pytest.fixture
def pressure(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    (home / 'config.yaml').write_text('system_load:\n  enabled: true\n  dwell_seconds: 0\ndelegation:\n  max_concurrent_children: 4\n')
    sample = [load.LoadSample(100, 12., 10, 0)]
    monkeypatch.setattr(load, 'sample_system_load', lambda: sample[0])
    monkeypatch.setattr(dt, '_load_config', lambda: {'max_concurrent_children': 4})
    monkeypatch.setattr(dispatch, '_finalize_child_results', lambda *args: None)
    monkeypatch.setattr(dispatch, '_report_child_done', lambda *args: None)
    return sample


def batch(n=4):
    tasks = [{'goal': f'task {i}', 'context': f'context {i}', 'output_schema': {'type': 'object'}} for i in range(n)]
    return dispatch._Batch(tasks, [(i, task, SimpleNamespace()) for i, task in enumerate(tasks)],
                           SimpleNamespace(session_id='parent'), {'model': None}, 'shared context', 'leaf', 4,
                           None, [], [], '', '', None, None, False, time.monotonic())


@pytest.mark.parametrize('load1,cap', [(12., 2), (21., 1), (None, 4), (0., 4)])
def test_sync_batch_preserves_input_but_bounds_actual_children(pressure, monkeypatch, load1, cap):
    pressure[0] = load.LoadSample(100, load1, 10, 0)
    active = 0
    peak = 0
    entered = threading.Event()
    overflow = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def child(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active > cap:
                overflow.set()
            if active == cap:
                entered.set()
        try:
            assert release.wait(5)
            return {'task_index': kwargs['task_index'], 'status': 'completed'}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(dt, '_run_single_child', child)
    assert dt._get_max_concurrent_children() == 4  # schema + accepted task count remain static
    work = batch()
    with ThreadPoolExecutor(1) as parent:
        future = parent.submit(contextvars.copy_context().run, dispatch._run_batch, work, False)
        try:
            assert entered.wait(5)
            overflow.wait(2)
        finally:
            release.set()
        result = json.loads(future.result(timeout=5))
    assert len(result['results']) == len(work.task_list)
    assert peak <= cap


@pytest.mark.parametrize('n', [1, 3])
def test_async_capacity_defers_without_inline_and_parent_can_retry(pressure, monkeypatch, n):
    work = batch(n)
    ran = []
    monkeypatch.setattr(dispatch, '_resolve_async_wake_sid', lambda *args: '')
    monkeypatch.setattr(dispatch, '_dispatch_unit', lambda *args: {'status': 'rejected', 'error': 'at capacity'})
    monkeypatch.setattr(dt, '_run_single_child', lambda **kw: ran.append(kw) or {'task_index': kw['task_index'], 'status': 'completed'})
    result = json.loads(dispatch._run_batch(work, True))
    assert result['status'] == 'deferred'
    assert result['retryable'] is True
    assert result['tasks'] == work.task_list
    assert result['context'] == work.context
    assert 'NOT run' in result['note']
    assert not ran
    pressure[0] = load.LoadSample(300, 5., 10, 0)
    retry = json.loads(dispatch._run_batch(work, True))
    assert len(retry['results']) == n
    assert 'SYNCHRONOUSLY' in retry['note']


@pytest.mark.parametrize('is_batch', [False, True])
def test_direct_async_admission_rejects_retryably_at_effective_capacity(pressure, monkeypatch, is_batch):
    from tools import async_delegation as ad
    ad._reset_for_tests()
    pressure[0] = load.LoadSample(100, 21., 10, 0)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_finalize = ad._finalize

    def finalize(*args):
        try:
            original_finalize(*args)
        finally:
            finished.set()

    monkeypatch.setattr(ad, '_finalize', finalize)

    def runner():
        entered.set()
        assert release.wait(5)
        return {'status': 'completed', 'results': []}

    call = ad.dispatch_async_delegation_batch if is_batch else ad.dispatch_async_delegation
    payload = {'goals': ['one', 'two']} if is_batch else {'goal': 'one'}
    kwargs = dict(**payload, context='must survive', toolsets=['file'], role='leaf', model=None,
                  session_key='parent', runner=runner, max_async_children=4)
    try:
        assert call(**kwargs)['status'] == 'dispatched'
        assert entered.wait(5)
        rejected = call(**kwargs)
        assert rejected['status'] == 'deferred'
        assert rejected['retryable'] and rejected['context'] == 'must survive'
        assert rejected['toolsets'] == ['file']
        assert all(rejected[k] == v for k, v in payload.items())
    finally:
        release.set()
        assert finished.wait(5)
        if ad._executor:
            ad._executor.shutdown(wait=True)
        ad._reset_for_tests()


def test_sync_single_defers_while_async_child_runs_and_preserves_request(pressure, monkeypatch):
    from tools import async_delegation as ad
    ad._reset_for_tests()
    pressure[0] = load.LoadSample(100, 21., 10, 0)
    entered, release = threading.Event(), threading.Event()
    def runner():
        entered.set()
        assert release.wait(5)
        return {'status': 'completed'}
    request = dict(goal='retry me', context='all the context', images=['/tmp/example.png'],
                   output_schema={'type': 'object'}, max_iterations=8, role='leaf', background=False)
    monkeypatch.setattr(dt, '_coerce_task_images', lambda *args: ([[]], None))
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *args: {'model': None})
    monkeypatch.setattr(dt, '_build_children', lambda *args, **kwargs: pytest.fail('must defer before construction'))
    try:
        assert ad.dispatch_async_delegation(goal='running', context=None, toolsets=None, role='leaf', model=None,
                                           session_key='parent', runner=runner, max_async_children=4)['status'] == 'dispatched'
        assert entered.wait(5)
        result = json.loads(dt.delegate_task(**request, parent_agent=SimpleNamespace(_delegate_depth=0)))
        assert result['status'] == 'deferred'
        assert result['request'] == request
    finally:
        release.set()
        if ad._executor:
            ad._executor.shutdown(wait=True)
        ad._reset_for_tests()


@pytest.mark.parametrize('n,load1,cap', [(1, 21., 1), (4, 12., 2), (4, 21., 1)])
def test_real_background_batch_counts_children_not_handles(pressure, monkeypatch, n, load1, cap):
    from tools import async_delegation as ad
    ad._reset_for_tests()
    pressure[0] = load.LoadSample(100, load1, 10, 0)
    entered, overflow, release = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak = 0, 0
    completed = []

    def child(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == cap:
                entered.set()
            if active > cap:
                overflow.set()
        try:
            assert release.wait(8)
            completed.append(kwargs['task_index'])
            return {'task_index': kwargs['task_index'], 'status': 'completed'}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(dt, '_run_single_child', child)
    monkeypatch.setattr(dispatch, '_resolve_async_wake_sid', lambda *args: '')
    work = batch(n)
    try:
        result = json.loads(dispatch._run_batch(work, True))
        assert result['status'] == 'dispatched'
        assert entered.wait(5)
        overflow.wait(2)
    finally:
        release.set()
        if ad._executor:
            ad._executor.shutdown(wait=True)
        ad._reset_for_tests()
    assert sorted(completed) == list(range(n))
    assert peak <= cap


def test_async_stall_clock_does_not_start_while_waiting_for_load_admission(pressure, monkeypatch):
    from tools import async_delegation as ad
    from tools import delegate_tool_config as config
    from tools.delegate_tool_admission import child_admission
    ad._reset_for_tests()
    pressure[0] = load.LoadSample(100, 21., 10, 0)
    release_executor, checked, ran = threading.Event(), threading.Event(), threading.Event()
    original_status = config.delegation_load_status

    def status(*args):
        checked.set()
        return original_status(*args)

    monkeypatch.setattr(config, 'delegation_load_status', status)
    with ThreadPoolExecutor(1) as pool:
        pool.submit(release_executor.wait, 10)
        monkeypatch.setattr(ad, '_get_executor', lambda *args: pool)
        result = ad.dispatch_async_delegation(goal='queued', context=None, toolsets=None,
                    role='leaf', model=None, session_key='parent', max_async_children=4,
                    runner=lambda: ran.set() or {'status': 'completed'})
        assert result['status'] == 'dispatched'
        try:
            with child_admission.slot(lambda: original_status(4)):
                checked.clear()
                release_executor.set()
                assert checked.wait(5)
                assert not ran.is_set()
                with ad._records_lock:
                    assert not ad._records[result['delegation_id']].get('_started')
        finally:
            release_executor.set()
        assert ran.wait(5)
    ad._reset_for_tests()


def test_batch_waiting_for_admission_is_not_mistaken_for_a_frozen_child(pressure, monkeypatch):
    from tools.delegate_tool_admission import child_admission
    pressure[0] = load.LoadSample(100, 21., 10, 0)
    checked, entered, release = threading.Event(), threading.Event(), threading.Event()
    original_status = dispatch.delegation_load_status
    work = batch(1)
    child = work.children[0][2]
    child.get_activity_summary = lambda: {'api_call_count': 0, 'current_tool': None, 'last_activity_ts': 0}

    def status(*args):
        checked.set()
        return original_status(*args)

    def run(**kwargs):
        entered.set()
        assert release.wait(5)
        return {'task_index': 0, 'status': 'completed'}

    monkeypatch.setattr(dispatch, 'delegation_load_status', status)
    monkeypatch.setattr(dt, '_run_single_child', run)
    with ThreadPoolExecutor(1) as pool:
        try:
            with child_admission.slot(lambda: original_status(4)):
                future = pool.submit(contextvars.copy_context().run, work.run_child, *work.children[0])
                assert checked.wait(5)
                first = dispatch._batch_progress_token([child])
                assert dispatch._batch_progress_token([child]) != first
            assert entered.wait(5)
            # Once the child runs, a frozen activity token MUST still be detectable.
            first = dispatch._batch_progress_token([child])
            assert dispatch._batch_progress_token([child]) == first
        finally:
            release.set()
        assert future.result(5)['status'] == 'completed'
