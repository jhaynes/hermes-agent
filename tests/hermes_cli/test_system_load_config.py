"""Registered policy keys reach runtime through the profile's real config loader."""
from dataclasses import asdict

from gateway.system_load import LoadPolicy
from hermes_cli.config import DEFAULT_CONFIG, _KNOWN_ROOT_KEYS, load_config_readonly
from hermes_constants import get_hermes_home


def test_policy_defaults_are_registered_and_runtime_overrides_reload(monkeypatch):
    from gateway import system_load as load
    from cron.scheduler_load import cron_load_status
    from tools.delegate_tool_config import delegation_load_status
    from hermes_cli.kanban_load import load_spawn_budget

    assert 'system_load' in _KNOWN_ROOT_KEYS
    assert DEFAULT_CONFIG['system_load'] == asdict(LoadPolicy())
    assert load_config_readonly()['system_load']['enabled'] is False
    home = get_hermes_home()
    monkeypatch.setattr(load, 'sample_system_load', lambda: load.LoadSample(100, 12., 10, 0))
    home.joinpath('config.yaml').write_text('''system_load:
  enabled: true
  elevated_cap_divisor: 3
  unbounded_elevated_cap: 3
cron:
  max_parallel_jobs: 0
''')
    assert delegation_load_status(9).effective_cap == 3
    assert cron_load_status().effective_cap == 3
    assert load_spawn_budget(9) == 1
    home.joinpath('config.yaml').write_text('system_load:\n  enabled: false\n')
    assert delegation_load_status(9).effective_cap == 9
    assert cron_load_status().effective_cap is None
    assert load_spawn_budget(9) == 9


def test_static_precedence_is_unchanged_when_load_gate_is_enabled(monkeypatch):
    from cron import scheduler
    from tools import delegate_tool
    from gateway import system_load as load
    from cron.scheduler_load import cron_load_status
    from tools.delegate_tool_config import delegation_load_status

    monkeypatch.setenv('DELEGATION_MAX_CONCURRENT_CHILDREN', '8')
    monkeypatch.setenv('HERMES_CRON_MAX_PARALLEL', '6')
    get_hermes_home().joinpath('config.yaml').write_text('''system_load:
  enabled: true
delegation:
  max_concurrent_children: 4
cron:
  max_parallel_jobs: 4
''')
    monkeypatch.setattr(load, 'sample_system_load', lambda: load.LoadSample(100, 12., 10, 0))
    assert delegate_tool._get_max_concurrent_children() == 4  # config first
    assert scheduler._resolve_max_parallel_workers() == 6  # legacy env first
    assert delegation_load_status().effective_cap == 2
    assert cron_load_status().effective_cap == 3
