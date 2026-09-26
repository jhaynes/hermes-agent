"""Cron: import path of the restart-safe external worker.

The worker is spawned through ``hermes_child_argv``: the install's runtime command on a
PM-managed install (which supplies the dependency generation), otherwise
``sys.executable -m cron.scheduler``. Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env -- the sanitizer's other decisions (dropped runtime
site-packages, dropped venv markers) stand.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

from hermes_cli._launchers import resolve_store_python, runtime_command


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def hermes_child_argv(module: str, args: list[str]) -> list[str]:
    """Argv for a Hermes-owned child running ``module`` from this install.

    On a PM-managed install ``sys.executable`` is the bare store Python: the gateway has its
    dependencies only because the launcher bootstrap put them on ``sys.path``, so a
    ``sys.executable -m`` child dies with ``No module named 'ruamel'``. The install's runtime
    command re-runs that bootstrap in the child. Elsewhere (developer venv, wheel/pipx) the
    interpreter carries its own packages and the legacy shape stays.
    """
    repo_root = Path(__file__).resolve().parent.parent
    store_python = resolve_store_python(repo_root)
    if store_python is not None:
        return runtime_command(repo_root, args, module=module, python=store_python)
    return [sys.executable, "-m", module, *args]


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` to the worker env's own PYTHONPATH (never ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([root, *existing]))
    return worker_env
