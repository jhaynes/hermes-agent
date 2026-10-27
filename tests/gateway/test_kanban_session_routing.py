"""The kanban.run routing fence + receipt fold (plan v3, gateway carrier).

The owner derives the session's route from the receipt the dispatcher minted
and CAS-linked to the live run (`gateway/session_kanban.py::build_kanban_policy`),
never from row-resident route mirrors or RPC params: a route exists iff a
validated receipt, bound to the live run, authorizes it. The fence fires only
for an enforced routed task — unrouted and shadow rows fold nothing, check
nothing, and keep the whole policy shape identical.

Fixtures mint and link real receipts through the split's own
``resolve_task_route`` against a real model_routing.db + kanban.db (RED
fixture direction: the tests assert the secure behavior and fail on the
unfenced base).
"""
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest


def _policy():
    return {
        "schema_version": 1, "policy_id": "kanban-default", "revision": 1,
        "approval_ref": "operator:test",
        "routes": [{
            "route_id": "openai-gpt5", "route_revision": 1, "provider": "openai",
            "model": "gpt-5", "endpoint": "https://api.openai.com/v1", "maker": "openai",
            "model_family": "gpt-5", "status": "approved", "allowed_roles": ["builder"],
            "capabilities": [], "verified_input_budget": 200000,
            "allowed_reasoning": ["low", "medium", "high"],
            "qualifications": ["shallow", "deep"], "assessment": "reviewed", "evidence": {},
        }],
        "rankings": {"builder": {"deep": ["openai-gpt5"], "shallow": ["openai-gpt5"]}},
    }


@pytest.fixture
def kanban_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env",
                        lambda name: str(tmp_path) if name == "default" else str(tmp_path / name))
    from hermes_cli import kanban_db as kb
    kb.create_board("owned")  # explicit creation; connect() never creates a board (#43243)
    return tmp_path


def _connection(tmp_path):
    from gateway.session_contract import Principal
    actor = Principal("owner", str(tmp_path), frozenset({"session:create"}), "transport")
    return SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)),
                           actor=actor, native_owner=True)


_CONFIG = {"model": {"default": "loop-model", "provider": "custom"},
           "platform_toolsets": {"cli": ["terminal", "file"]},
           "agent": {"coding_context": "focus"}}


def _create_routed_task(conn, workspace, *, model_override=None, reasoning_effort=None,
                        routing_mode="enforced", routing_role="builder"):
    from hermes_cli import kanban_db as kb
    return kb.create_task(conn, title="routed card", assignee="default",
        workspace_kind="dir", workspace_path=str(workspace),
        model_override=model_override, reasoning_effort=reasoning_effort,
        routing_role=routing_role, routing_mode=routing_mode,
        routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192})


def _claim_and_mint(home, conn, task_id):
    """The dispatcher's real mint path: claim, then resolve_task_route mints and
    CAS-links the receipt to the live run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_routing import resolve_task_route
    from agent.model_selection_store import activate_policy, publish_policy
    kb.recompute_ready(conn)
    task = kb.claim_task(conn, task_id)
    record = publish_policy(home, _policy(), approval_ref="operator:test")
    activate_policy(home, "kanban-default", record["revision"])
    kwargs = resolve_task_route(home, conn, kb.get_task(conn, task_id),
                                now=1000, frozen_sha="deadbeef", verified_by="test")
    return task, kwargs["receipt_id"]


def _build_policy(tmp_path, conn, task):
    from gateway.session_kanban import build_kanban_policy
    params = dict(board="owned", task_id=task.id, run_id=task.current_run_id,
                  claim_lock=task.claim_lock)
    return build_kanban_policy(_connection(tmp_path), params, _CONFIG)


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


def test_receipted_route_folds_into_policy(kanban_env):
    """The receipted route (provider/model/reasoning) reaches the owner-built
    policy by derivation from the receipt; no base_url is folded and the
    kanban_json context carries the receipt id for the worker's gate."""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env), reasoning_effort="high")
        task, receipt_id = _claim_and_mint(kanban_env, conn, tid)
    policy, _secrets = _build_policy(kanban_env, conn, task)
    assert policy.provider == "openai" and policy.model == "gpt-5"
    assert policy.config()["agent"]["reasoning_effort"] == "high"
    assert policy.base_url is None  # no base_url is folded, ever
    context = json.loads(policy.kanban_json)
    assert context["routing_receipt_id"] == receipt_id
    from agent.model_selection_store import get_receipt
    decision = get_receipt(get_hermes_home(), receipt_id)
    assert decision["selected"]["model"] == "gpt-5"


def test_receipt_wins_over_user_override(kanban_env, caplog):
    """A user model_override disagreeing with the receipt never wins the live
    attempt: the receipt wins, and the dropped override is logged with the
    field, the override value and the receipt id — never silent, never blocking."""
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env), model_override="user-model")
        task, receipt_id = _claim_and_mint(kanban_env, conn, tid)
    with caplog.at_level("WARNING"):
        policy, _secrets = _build_policy(kanban_env, conn, task)
    assert policy.model == "gpt-5"  # the receipt wins the live attempt
    assert policy.provider == "openai"
    line = caplog.text
    assert "model" in line and "user-model" in line and receipt_id in line


def test_enforced_task_without_receipt_fails_closed(kanban_env):
    """An enforced task with no receipt link authorizes nothing: refused with
    routing_blocked before any session exists, never folded onto the default."""
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env))
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
    with pytest.raises(RuntimeStoreError) as excinfo:
        _build_policy(kanban_env, conn, task)
    exc = excinfo.value
    assert exc.reason == "routing_blocked" and exc.detail == "receipt_missing"
    assert getattr(exc, "wire_message", "") == "routing_blocked_receipt_missing"


def test_unresolvable_receipt_fails_closed(kanban_env):
    """A linked receipt id that resolves to nothing in the owner's own store is
    refused, never treated as unrouted."""
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env))
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET routing_receipt_id='rr_bogus' WHERE id=?", (tid,))
        conn.commit()
    with pytest.raises(RuntimeStoreError, match="routing_blocked"):
        _build_policy(kanban_env, conn, task)


def test_receipt_bound_to_another_attempt_is_blocked(kanban_env):
    """Mutant: the task row's receipt id swapped for another task's valid
    receipt — the binding check (execution_id/attempt_id) refuses it."""
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        foreign = _create_routed_task(conn, _workspace(kanban_env))
        _task, foreign_receipt = _claim_and_mint(kanban_env, conn, foreign)
        tid = _create_routed_task(conn, _workspace(kanban_env))
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET routing_receipt_id=? WHERE id=?", (foreign_receipt, tid))
        conn.commit()
    with pytest.raises(RuntimeStoreError) as excinfo:
        _build_policy(kanban_env, conn, task)
    assert excinfo.value.reason == "routing_blocked"
    assert excinfo.value.detail == "stale_attempt"


def test_role_removal_ignores_stale_receipt(kanban_env):
    """Rollback clears the role, not the receipt column: attempt N+1 runs
    unrouted — no block, no fold — even though routing_receipt_id still names
    run N's receipt."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env))
        task, _receipt_id = _claim_and_mint(kanban_env, conn, tid)
        conn.execute("UPDATE tasks SET routing_role=NULL, routing_mode=NULL WHERE id=?", (tid,))
        conn.commit()
    policy, _secrets = _build_policy(kanban_env, conn, task)
    assert policy.model == "loop-model" and policy.provider == "custom"  # config default, no fold
    assert "routing_receipt_id" not in json.loads(policy.kanban_json)


def test_shadow_task_with_stale_enforced_receipt_is_untouched(kanban_env):
    """A shadow row folds nothing and checks nothing — even carrying a stale
    enforced receipt id from an earlier attempt."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        foreign = _create_routed_task(conn, _workspace(kanban_env))
        _task, foreign_receipt = _claim_and_mint(kanban_env, conn, foreign)
        tid = _create_routed_task(conn, _workspace(kanban_env), routing_mode="shadow")
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET routing_receipt_id=? WHERE id=?", (foreign_receipt, tid))
        conn.commit()
    policy, _secrets = _build_policy(kanban_env, conn, task)
    assert policy.model == "loop-model"
    assert "routing_receipt_id" not in json.loads(policy.kanban_json)


def test_revoked_receipt_blocks_at_admission(kanban_env):
    """An emergency revocation before first admission blocks the fence."""
    from hermes_state_runtime import RuntimeStoreError
    from agent.model_selection_store import revoke_route
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env))
        task, _receipt_id = _claim_and_mint(kanban_env, conn, tid)
        revoke_route(kanban_env, "kanban-default", route_id="openai-gpt5",
                     reason="incident", approval_ref="operator:revoke")
    with pytest.raises(RuntimeStoreError) as excinfo:
        _build_policy(kanban_env, conn, task)
    assert excinfo.value.reason == "routing_blocked"
    assert excinfo.value.detail == "stale_or_revoked_decision"


def test_owner_reads_only_its_own_store(kanban_env):
    """Under the same-home ruling the owner reads only its own home's receipt
    store: a receipt id that exists only in another home is receipt_missing."""
    from hermes_state_runtime import RuntimeStoreError
    from agent.model_selection import select
    from agent.model_selection_store import activate_policy, persist_receipt, publish_policy
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    other_home = kanban_env / "other-home"
    other_home.mkdir()
    record = publish_policy(other_home, _policy(), approval_ref="operator:test")
    activate_policy(other_home, "kanban-default", record["revision"])
    requirements = {
        "schema_version": 1, "role": "builder", "execution_kind": "kanban",
        "execution_id": "t_foreign", "attempt_id": "1", "slot_id": "",
        "task_class": "", "required_capabilities": [], "input_tokens": 1000,
        "reserve_tokens": 8192, "reasoning": "medium",
        "provenance": {"frozen_sha": "deadbeef", "verified_by": "test",
                       "complete": True, "contributors": []},
    }
    foreign_receipt = persist_receipt(other_home, select(requirements, _policy(), {}, now=1000))
    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env))
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET routing_receipt_id=? WHERE id=?", (foreign_receipt, tid))
        conn.commit()
    with pytest.raises(RuntimeStoreError) as excinfo:
        _build_policy(kanban_env, conn, task)
    assert excinfo.value.detail == "receipt_missing"


def test_unrouted_task_policy_shape_identical(kanban_env):
    """An unrouted task's policy and context keep the exact pre-carrier shape:
    the user-override fold is intact and no routing key enters the context."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = _create_routed_task(conn, _workspace(kanban_env), model_override="loop-model")
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET routing_role=NULL, routing_mode=NULL, routing_receipt_id=NULL WHERE id=?", (tid,))
        conn.commit()
    policy, _secrets = _build_policy(kanban_env, conn, task)
    assert policy.model == "loop-model" and policy.provider == "custom"
    context = json.loads(policy.kanban_json)
    assert set(context) == {"board", "task_id", "run_id", "claim_lock", "workspace", "branch",
                            "tenant", "profile", "workspaces_root", "skills", "goal_mode",
                            "goal_max_turns", "goal_text", "context", "max_runtime_seconds",
                            "accept_hooks", "db"}
