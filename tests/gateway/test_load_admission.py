"""Profile-scoped admission over real config loaders and thread context hops."""
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import system_load as load
from gateway.load_admission import LoadAdmission
from cron.scheduler_load import cron_load_status
from tools.delegate_tool_config import delegation_load_status
from hermes_cli.kanban_load import load_spawn_budget
from cron.scheduler_provider import _profile_cron_scope
from agent.secret_scope import set_multiplex_active


def test_all_seams_keep_profile_policy_and_hysteresis_isolated(tmp_path, monkeypatch):
    a, b = tmp_path / 'a', tmp_path / 'b'
    for home, enabled in ((a, True), (b, False)):
        home.mkdir()
        (home / 'config.yaml').write_text(json.dumps({
            'system_load': {'enabled': enabled}, 'cron': {'max_parallel_jobs': 4},
            'delegation': {'max_concurrent_children': 4}}))
    sample: list[load.LoadSample | None] = [load.LoadSample(100, 12., 10, 0)]
    monkeypatch.setattr(load, 'sample_system_load', lambda: sample[0])

    def caps():
        return (delegation_load_status().effective_cap, cron_load_status().effective_cap,
                load_spawn_budget(4))

    set_multiplex_active(True)
    try:
        with _profile_cron_scope(a):
            assert caps() == (2, 2, 1)
        with _profile_cron_scope(b):
            with ThreadPoolExecutor(1) as worker:
                assert worker.submit(contextvars.copy_context().run, caps).result(5) == (4, 4, 4)
        sample[0] = load.LoadSample(110, 7., 10, 0)
        with _profile_cron_scope(a):
            assert caps() == (2, 2, 1)  # B did not erase A's dwell
        sample[0] = None
        with _profile_cron_scope(a):
            assert caps() == (4, 4, 4)  # total sampling failure is no opinion
    finally:
        set_multiplex_active(False)


@pytest.mark.parametrize('recovery', ['disabled', 'unknown', 'ok'])
def test_waiting_admission_rechecks_policy_and_never_blocks_another_home(tmp_path, monkeypatch, recovery):
    gate = LoadAdmission()
    a, b = tmp_path / 'a', tmp_path / 'b'
    for home in (a, b):
        home.mkdir()
        (home / 'config.yaml').write_text('system_load:\n  enabled: true\n  dwell_seconds: 0\n')
    sample: list[load.LoadSample | None] = [load.LoadSample(100, 21., 10, 0)]
    monkeypatch.setattr(load, 'sample_system_load', lambda: sample[0])
    started, checked = threading.Event(), threading.Event()

    def status():
        result = delegation_load_status(4)
        checked.set()
        return result

    def waiter():
        with gate.slot(status):
            started.set()

    with ThreadPoolExecutor(1) as pool:
        with _profile_cron_scope(a):
            with gate.slot(lambda: delegation_load_status(4)):
                future = pool.submit(contextvars.copy_context().run, waiter)
                assert checked.wait(5)
                with _profile_cron_scope(b):
                    assert not gate.at_capacity(delegation_load_status(4))
                assert gate.at_capacity(delegation_load_status(4))
                if recovery == 'disabled':
                    (a / 'config.yaml').write_text('system_load:\n  enabled: false\n')
                else:
                    sample[0] = None if recovery == 'unknown' else load.LoadSample(300, 0., 10, 0)
                assert started.wait(5)
            future.result(5)
        assert not gate._active
