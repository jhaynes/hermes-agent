"""Load admission contracts: thresholds are strict; missing data is no opinion."""
import importlib

import pytest


def test_classification_hysteresis_and_swap_tiebreak():
    load = importlib.import_module('gateway.system_load')
    policy = load.LoadPolicy(enabled=True)
    state = load.LoadState()

    def classify(value, ts, swap=0):
        nonlocal state
        level, state = load.classify(load.LoadSample(ts, value, 10, swap), policy, state)
        return level

    assert classify(10, 0) == 'ok'
    assert classify(11, 1) == 'elevated'
    for ts, value in [(10, 9), (20, 11), (30, 7), (120, 7)]:
        assert classify(value, ts) == 'elevated'
    assert classify(8, 121) == 'elevated'
    assert classify(7, 122) == 'ok'
    assert classify(20, 123) == 'elevated'
    assert classify(21, 124) == 'critical'
    assert classify(14, 200) == 'critical'
    assert classify(15, 244) == 'critical'
    assert classify(14, 245) == 'elevated'
    assert classify(7, 366) == 'ok'
    assert classify(5, 367, 99) == 'ok'  # old swap alone is not pressure
    assert classify(12, 368, 91) == 'critical'
    assert load.classify(None, policy, state) == ('unknown', state)
    for value in (None, -1, True, float('nan'), float('inf')):
        assert load.classify(load.LoadSample(400, value, 10, 99), policy, state) == ('unknown', state)
    for cores in (None, 0, -1, True, float('nan'), float('inf')):
        assert load.classify(load.LoadSample(400, 12, cores, 99), policy, state) == ('unknown', state)
    assert load.classify(load.LoadSample(400, 100, 10, 99), load.LoadPolicy(), state)[0] == 'ok'


def test_policy_validation_defaults_invalid_fields_and_orders_tiers(caplog):
    from gateway import system_load as load
    from dataclasses import asdict
    defaults = asdict(load.LoadPolicy())
    for field in defaults:
        for bad in (None, [], {}, 'garbage', -1, float('nan'), float('inf')):
            assert getattr(load.validate_policy({field: bad}), field) == defaults[field]
        if field != 'enabled':
            assert getattr(load.validate_policy({field: True}), field) == defaults[field]
    assert load.validate_policy({'enabled': 'true', 'dwell_seconds': '0'}).enabled
    assert load.validate_policy({'dwell_seconds': '0'}).dwell_seconds == 0
    assert load.validate_policy({'elevated_cap_divisor': 2.5}).elevated_cap_divisor == 2
    for raw in ({'elevated_exit_ratio': 5}, {'critical_exit_ratio': 5},
                {'elevated_enter_ratio': 3}, {'critical_enter_ratio': .5}):
        p = load.validate_policy(raw)
        assert p.elevated_exit_ratio < p.elevated_enter_ratio < p.critical_enter_ratio
        assert p.elevated_exit_ratio < p.critical_exit_ratio < p.critical_enter_ratio
    assert 'system_load' in caplog.text


def test_sampler_caches_raw_partial_samples(monkeypatch):
    from gateway import system_load as load
    import os
    import psutil
    from types import SimpleNamespace
    clock = [100.0]
    calls = []

    def broken():
        raise OSError('unavailable')

    monkeypatch.setattr(load, '_sample', None)
    monkeypatch.setattr(load, '_now', lambda: clock[0])
    monkeypatch.setattr(os, 'getloadavg', lambda: calls.append(1) or (12., 10., 10.), raising=False)
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(cpu_affinity=lambda: [0, 1]))
    monkeypatch.setattr(psutil, 'swap_memory', broken)
    monkeypatch.setattr(load, '_sysctl_swap', lambda: None)
    first = load.sample_system_load()
    assert (first.load1, first.cores, first.swap_used_pct) == (12., 2, None)
    clock[0] += 29
    assert load.sample_system_load() is first
    assert len(calls) == 1
    clock[0] += 1
    monkeypatch.setattr(os, 'getloadavg', broken)
    monkeypatch.setattr(psutil, 'Process', broken)
    monkeypatch.setattr(os, 'cpu_count', lambda: 10)
    monkeypatch.setattr(psutil, 'swap_memory', lambda: SimpleNamespace(percent=20))
    second = load.sample_system_load()
    assert (second.load1, second.cores, second.swap_used_pct) == (None, 10, 20)


@pytest.mark.macos_only
def test_macos_swap_fallback_is_bounded_and_fail_open(monkeypatch):
    from gateway import system_load as load
    import subprocess

    def probe(argv, **kwargs):
        assert argv == ['sysctl', '-n', 'vm.swapusage']
        assert kwargs['timeout'] == 2
        kwargs['stdout'].write(b'total = 10.00G  used = 9.00G  free = 1.00G')
    monkeypatch.setattr(subprocess, 'run', probe)
    assert load._sysctl_swap() == 90

    def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 2)
    monkeypatch.setattr(subprocess, 'run', broken)
    assert load._sysctl_swap() is None


def test_runtime_state_and_live_config_are_profile_scoped(tmp_path, monkeypatch, caplog):
    from gateway import system_load as load
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_cli.config import load_config_readonly
    from concurrent.futures import ThreadPoolExecutor
    import contextvars
    import logging
    import json

    a, b = tmp_path / 'a', tmp_path / 'b'
    for home, enabled in ((a, True), (b, False)):
        home.mkdir()
        (home / 'config.yaml').write_text(json.dumps({'system_load': {'enabled': enabled}}))
    sample = [load.LoadSample(100, 12., 10, 0)]
    monkeypatch.setattr(load, 'sample_system_load', lambda: sample[0])
    monkeypatch.setattr(load, '_now', lambda: sample[0].ts + 3)
    caplog.set_level(logging.INFO, logger=load.__name__)

    def decide():
        return load.load_status('test', 10, lambda base, level, p: base // 2 if level == 'elevated' else base)

    with _profile_cron_scope(a):
        assert load_config_readonly()['system_load']['enabled'] is True
        with ThreadPoolExecutor(3) as pool:
            statuses = [f.result() for f in [pool.submit(contextvars.copy_context().run, decide) for _ in range(6)]]
        assert all(s.effective_cap == 5 for s in statuses)
        assert statuses[0].sample_age == 3
    with _profile_cron_scope(b):
        assert decide().effective_cap == 10
    sample[0] = load.LoadSample(110, 9., 10, 0)
    with _profile_cron_scope(a):
        assert decide().effective_cap == 5
        (a / 'config.yaml').write_text('system_load:\n  enabled: false\n')
        assert decide().effective_cap == 10
    transitions = [r.message for r in caplog.records if 'load-throttle:' in r.message]
    assert len(transitions) == 2
    assert 'load1' in transitions[0] and 'sample' in transitions[0]
    assert 'recovered' in transitions[1]


def _assert_live_sample():
    from gateway import system_load as load
    load._sample = None
    sample = load.sample_system_load()
    assert sample.cores is not None and sample.cores >= 1
    assert sample.load1 is not None and sample.load1 >= 0
    assert sample.swap_used_pct is None or 0 <= sample.swap_used_pct <= 100


@pytest.mark.macos_only
def test_macos_live_sampler():
    _assert_live_sample()


@pytest.mark.linux_only
def test_linux_live_sampler():
    _assert_live_sample()


def test_total_probe_exception_is_unknown_not_a_disabled_policy(monkeypatch):
    from gateway import system_load as load
    from hermes_constants import get_hermes_home
    from tools.delegate_tool_config import delegation_load_status
    get_hermes_home().joinpath('config.yaml').write_text('system_load:\n  enabled: true\n')

    def failed():
        raise OSError('host metrics unavailable')

    monkeypatch.setattr(load, 'sample_system_load', failed)
    status = delegation_load_status(4)
    assert status.level == 'unknown'
    assert status.policy.enabled
    assert status.effective_cap == 4
