"""Hermes-owned cron children start through the install's runtime command.

On a PM-managed install the gateway's ``sys.executable`` is the bare store Python;
its dependencies are on ``sys.path`` only because the launcher bootstrap put them
there. A child spawned as ``sys.executable -m <module>`` gets none of them and dies
with ``No module named 'ruamel'`` before its ownership ack.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import cron.scheduler_worker_env as worker_env

REPO_ROOT = Path(worker_env.__file__).resolve().parent.parent


def test_managed_install_uses_runtime_command(monkeypatch, tmp_path):
    store = tmp_path / "store" / "bin" / "python3"
    monkeypatch.setattr(worker_env, "resolve_store_python", lambda _root: store)

    argv = worker_env.hermes_child_argv("cron.scheduler", ["--external-worker-file", "p"])

    assert argv[:3] == [str(store), "-I", "-c"]
    assert "import hermes_bootstrap" in argv[3]
    assert "'cron.scheduler'" in argv[3]
    assert argv[4:] == ["--external-worker-file", "p"]


def test_unmanaged_install_keeps_the_legacy_argv(monkeypatch):
    monkeypatch.setattr(worker_env, "resolve_store_python", lambda _root: None)

    argv = worker_env.hermes_child_argv("hermes_cli.main", ["-p", "x"])

    assert argv == [sys.executable, "-m", "hermes_cli.main", "-p", "x"]


def test_worker_entry_accepts_runtime_command_shaped_argv(tmp_path):
    """Under ``-c`` + runpy the worker must still see ``--external-worker-file`` and take the
    worker branch (a missing payload fails with exit 1), never fall through to ``tick()``."""
    from hermes_cli._launchers import runtime_command

    home = tmp_path / "home"
    home.mkdir()
    ack = tmp_path / "exec.ready"
    argv = runtime_command(
        REPO_ROOT, ["--external-worker-file", str(tmp_path / "missing.json"), "--ack-file", str(ack)],
        module="cron.scheduler", python=sys.executable, home=home)

    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(home)})

    assert result.returncode == 1, result.stderr[-2000:]
    assert not ack.exists()
    assert not (home / "cron" / ".tick.lock").exists()


def _live_runtime() -> tuple[str, str] | None:
    """(store python, HERMES_HOME) captured at import: the autouse fixture swaps HERMES_HOME
    for a temp dir before the test body runs, which hides the developer's PM state."""
    import os

    try:
        from hermes_cli._launchers import resolve_store_python
        from pm.environments import committed_venv
        store = resolve_store_python(REPO_ROOT)
        if store is None or committed_venv(REPO_ROOT) is None:
            return None
        return str(store), os.environ["HERMES_HOME"]
    except Exception:
        return None


_LIVE = _live_runtime()


@pytest.mark.platforms("posix")
@pytest.mark.skipif(_LIVE is None,
                    reason="needs a PM store python and a committed dependency generation for this tree")
def test_live_helper_argv_imports_dependencies_bare_store_python_cannot(tmp_path, monkeypatch):
    import os

    store, home = _LIVE
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setattr(worker_env, "resolve_store_python", lambda _root: Path(store))
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", str(tmp_path)),
           "HERMES_HOME": home, "PYTHONPATH": str(REPO_ROOT), "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    bare = subprocess.run([store, "-m", "hermes_yaml"], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    helper = subprocess.run(worker_env.hermes_child_argv("hermes_yaml", []), env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)

    assert bare.returncode != 0 and "ruamel" in bare.stderr
    assert helper.returncode == 0, helper.stderr[-2000:]


@pytest.mark.platforms("linux")
def test_restart_safe_scope_keeps_the_helper_argv_intact_at_its_tail(monkeypatch, tmp_path):
    import tools.process_registry as process_registry

    monkeypatch.setattr(worker_env, "resolve_store_python", lambda _root: tmp_path / "python3")
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setenv("INVOCATION_ID", "managed-service")
    monkeypatch.setattr(process_registry, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    command = worker_env.hermes_child_argv("cron.scheduler", ["--external-worker-file", "p", "--ack-file", "a"])

    dispatch = process_registry.restart_safe_gateway_child_argv(
        command, unit_suffix="cron-job-1", require_restart_safe_scope=True)

    assert dispatch.mode == "scoped"
    assert dispatch.argv[0] == "/usr/bin/systemd-run"
    assert dispatch.argv[-len(command):] == command


def test_launch_external_worker_spawns_the_helper_argv(monkeypatch, tmp_path):
    import cron.scheduler as scheduler
    from tools.process_registry import GatewayChildDispatch

    store = tmp_path / "python3"
    monkeypatch.setattr(worker_env, "resolve_store_python", lambda _root: store)
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("tools.process_registry.restart_safe_gateway_child_argv",
                        lambda command, **_: GatewayChildDispatch("degraded", command))
    spawned = []

    class _Stop(Exception):
        pass

    def popen(command, **_kwargs):
        spawned.append(command)
        raise _Stop

    monkeypatch.setattr(scheduler, "mark_execution_handoff_pending", lambda _e: {"id": "exec-1"})
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)

    with pytest.raises(_Stop):
        scheduler._launch_external_cron_worker({"id": "job-1", "execution_id": "exec-1", "prompt": "w"})

    handoff = tmp_path / "cron" / "external-workers"
    assert spawned[0] == worker_env.hermes_child_argv("cron.scheduler", [
        "--external-worker-file", str(handoff / "exec-1.json"),
        "--ack-file", str(handoff / "exec-1.ready")])
    assert spawned[0][0] == str(store)


def test_bot_chat_relay_uses_the_helper_argv(monkeypatch, tmp_path):
    from unittest import mock

    from cron import scheduler_delivery as sched_delivery

    store = tmp_path / "python3"
    monkeypatch.setattr(worker_env, "resolve_store_python", lambda _root: store)
    calls = {}

    def fake_run(argv, env, report_path, timeout):
        calls["argv"] = argv
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with mock.patch.object(sched_delivery, "_run_bot_chat_turn", side_effect=fake_run):
        err = sched_delivery._deliver_to_bot_chat({"id": "j1", "name": "Digest"}, "out", "")

    assert err is None
    prefix = worker_env.hermes_child_argv("hermes_cli.main", [])
    assert calls["argv"][:len(prefix)] == prefix
    assert calls["argv"][0] == str(store)
