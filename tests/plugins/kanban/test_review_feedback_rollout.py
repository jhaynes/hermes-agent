"""Staged reviewer profiles use supported creation/configuration in a temp home."""
import json
from pathlib import Path

from hermes_cli.profiles import create_profile
from hermes_cli.config import load_config, set_config_value
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_rollout_profiles_are_creatable_without_clone_or_capacity_increase(tmp_path, monkeypatch):
    home=tmp_path/'.hermes'
    home.mkdir()
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HERMES_HOME',str(home))
    root=Path(__file__).resolve().parents[3]
    manifest=json.loads((root/'plugins/kanban/review_feedback/rollout.json').read_text())
    assert {p['mandate'] for p in manifest['profiles']}=={'breaker_a','breaker_b','breaker_c','scope'}
    assert not any('max_in_progress' in key for key in manifest['settings'])
    paths=[]
    for profile in manifest['profiles']:
        path=create_profile(profile['name'],description=profile['description'],**manifest['creation_options'])
        paths.append(path)
        assert path.is_relative_to(home/'profiles')
        token=set_hermes_home_override(path)
        try:
            for key,value in manifest['settings'].items():
                set_config_value(key,json.dumps(value))
            config=load_config()
            assert 0<config['agent']['max_turns']<500
            assert config['platform_toolsets']['cli']==manifest['settings']['platform_toolsets.cli']
            assert 'delegation' in config['agent']['disabled_toolsets']
        finally:
            reset_hermes_home_override(token)
    assert len(set(paths))==len(manifest['profiles'])
