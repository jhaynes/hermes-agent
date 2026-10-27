"""E2E: guided-routing Kanban vertical slice (plans/2026-09-15_141016-
guided-model-routing.md). Isolated temp Hermes home + real sqlite files
(kanban.db, model_routing.db) + a real dispatch_once() tick — no mocks of
the routing library itself, only the spawn subprocess is stubbed (a real
child process is out of scope for this test and is exercised by the
existing kanban worker-argv tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.model_selection_store import get_receipt, publish_policy, activate_policy
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _policy():
    return {
        "schema_version": 1,
        "policy_id": "kanban-default",
        "revision": 1,
        "routes": [
            {
                "route_id": "openai-gpt5", "route_revision": 1, "provider": "openai",
                "model": "gpt-5", "endpoint": "https://api.openai.com/v1", "maker": "openai",
                "model_family": "gpt-5", "status": "approved",
                "allowed_roles": ["builder"], "capabilities": [],
                "verified_input_budget": 200000, "allowed_reasoning": ["low", "medium", "high"],
                "qualifications": ["shallow", "deep"], "assessment": "reviewed", "evidence": {},
            },
        ],
        "rankings": {"builder": {"deep": ["openai-gpt5"], "shallow": ["openai-gpt5"]}},
    }


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_managed_task_resolves_receipted_route_and_spawns_with_it(
    kanban_home, all_assignees_spawnable,
):
    from hermes_constants import get_hermes_home

    record = publish_policy(get_hermes_home(), _policy(), approval_ref="operator:test-e2e")
    activate_policy(get_hermes_home(), "kanban-default", record["revision"])

    captured = {}

    def _fake_spawn(task, workspace):
        import os
        # The carrier carries nothing on the claimed attempt: the spawn sees no
        # stamped overrides; the receipted route is derived by the owner from the
        # receipt linked on the task row.
        captured["model"] = task.model_override
        captured["provider"] = task.provider_override
        return os.getpid()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="managed card", assignee="alice", routing_role="builder",
            routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192},
        )
        res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=_fake_spawn)
        task = kb.get_task(conn, tid)

    assert res.spawned == [(tid, "alice", "")] or res.spawned[0][0] == tid
    # The claimed attempt carries no stamped route (ruling 2): the override
    # columns stay user-owned; the owner derives the route from the receipt.
    assert captured["model"] is None and captured["provider"] is None
    # A real receipt id was persisted on the task row and resolves to the
    # exact decision that was applied.
    assert task.routing_receipt_id
    decision = get_receipt(get_hermes_home(), task.routing_receipt_id)
    assert decision["selected"]["provider"] == "openai"
    assert decision["selected"]["model"] == "gpt-5"
    assert decision["requirements"]["role"] == "builder"
    assert decision["requirements"]["target_profile"] == "alice"
    from agent.model_selection_store import append_outcome
    import time
    append_outcome(kanban_home, task.routing_receipt_id, "routing_health", {
        "target_profile": "alice", "route_revision": 1,
        "endpoint": "https://api.openai.com/v1", "status": "outage",
        "observed_at": int(time.time()), "retry_after": 0,
    })
    with kbc.connect() as conn:
        kb.create_task(conn, title="same unavailable route", assignee="alice", routing_role="builder",
                       routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192})
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, reconcile_orphans=False)
    assert res.spawned == []


def test_cross_home_routed_dispatch_refused(kanban_home, all_assignees_spawnable, monkeypatch):
    """Ruling 1 (same-home): a dispatcher whose origin home differs from the
    assignee profile's home refuses a routed dispatch before minting — no
    receipt, no link, claim released through the normal failure path. Shadow
    dispatch is exempt (it mints only a shadow receipt and authorizes nothing)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from agent.model_selection_store import activate_policy, publish_policy
    from hermes_constants import get_hermes_home

    record = publish_policy(get_hermes_home(), _policy(), approval_ref="operator:test")
    activate_policy(get_hermes_home(), "kanban-default", record["revision"])
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env",
                        lambda name: str(kanban_home / "other-home"))
    spawned = []

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="cross-home card", assignee="alice", routing_role="builder",
            routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192},
        )
        res = kbd.dispatch_once(conn, dry_run=False,
                                spawn_fn=lambda task, workspace: spawned.append(task.id))
        task = kb.get_task(conn, tid)

    assert spawned == [] and res.spawned == []
    assert task.routing_receipt_id is None  # nothing minted, nothing linked
    assert "cross-home" in (task.last_failure_error or "")
    assert task.status in ("ready", "blocked")  # claim released to the failure path

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="cross-home shadow card", assignee="alice", routing_role="builder",
            routing_mode="shadow",
            routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192},
        )
        res = kbd.dispatch_once(conn, dry_run=False, reconcile_orphans=False,
                                spawn_fn=lambda task, workspace: spawned.append(task.id))
        task = kb.get_task(conn, tid)

    assert res.spawned, "shadow dispatch is exempt from the same-home refusal"


def test_missing_profile_dir_assignee_is_same_home_and_owner_backstop_refuses(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The declared §2.1 deviation: an assignee whose profile dir does not exist
    (``resolve_profile_env`` FileNotFoundError) is NOT cross-home — the spawn env
    runs that worker in the origin home, so the store the fence reads IS the store
    the worker's HERMES_HOME names. The dispatcher therefore mints normally, and
    the owner's own backstop refuses the claim fail-closed with
    invalid_kanban_claim instead of admitting a cross-profile session."""
    from types import SimpleNamespace

    from gateway.session_kanban import build_kanban_policy
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import RuntimeStoreError

    def _no_profile_dir(name):
        raise FileNotFoundError(name)

    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", _no_profile_dir)
    record = publish_policy(get_hermes_home(), _policy(), approval_ref="operator:test")
    activate_policy(get_hermes_home(), "kanban-default", record["revision"])

    def _spawn(task, workspace):
        import os
        return os.getpid()

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="no-profile-dir card", assignee="alice", routing_role="builder",
            routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192},
        )
        res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=_spawn)
        task = kb.get_task(conn, tid)

    assert res.spawned, "no-profile-dir assignee is same-home by construction, not refused"
    assert task.routing_receipt_id  # minted and linked like any same-home routed dispatch

    connection = SimpleNamespace(
        authority=SimpleNamespace(profile_id=str(kanban_home)),
        actor=SimpleNamespace(profile_id=str(kanban_home),
                              capabilities=frozenset({"session:create"})),
        native_owner=True)
    params = dict(board=kb.DEFAULT_BOARD, task_id=tid, run_id=task.current_run_id,
                  claim_lock=task.claim_lock)
    with pytest.raises(RuntimeStoreError) as excinfo:
        build_kanban_policy(connection, params, {"model": {"default": "loop-model",
                                                           "provider": "custom"}})
    assert excinfo.value.reason == "invalid_kanban_claim"  # the owner's fail-closed backstop


def test_unmanaged_task_is_never_touched_by_routing(kanban_home, all_assignees_spawnable):
    """No routing_role -> zero guided-routing involvement; existing
    model_override/provider_override behavior is untouched."""
    captured = {}

    def _fake_spawn(task, workspace):
        captured["model"] = task.model_override
        return 99

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain card", assignee="alice")
        kbd.dispatch_once(conn, dry_run=False, spawn_fn=_fake_spawn)
        task = kb.get_task(conn, tid)

    assert captured["model"] is None
    assert task.routing_receipt_id is None


def test_managed_task_with_no_active_policy_fails_closed_not_default_route(
    kanban_home, all_assignees_spawnable,
):
    """No published/active policy -> spawn fails via the normal breaker path;
    the task must NOT silently fall back to an unmanaged default route
    (design §5)."""
    spawned = []

    def _fake_spawn(task, workspace):
        spawned.append(task.id)
        return 1

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="managed card, no policy", assignee="alice", routing_role="builder",
            max_retries=1,
        )
        res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=_fake_spawn)
        task = kb.get_task(conn, tid)

    assert spawned == []
    assert res.spawned == []
    assert task.routing_receipt_id is None
    assert task.status in ("blocked", "ready")  # auto-blocked by the breaker, or requeued for retry
