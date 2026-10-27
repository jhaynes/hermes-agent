"""The kanban.run worker-session gate (plan v3 §2.4/§2.6): admission re-check,
worker-side attempt binding, pre-request-1 enforcement and fallback containment.

Admission-level tests drive the real ``run_task`` against a real kanban.db with
stubbed session plumbing (the same harness
``test_session_kanban.py::test_claim_reclaimed_before_admission_marker_is_refused``
uses). Worker-side tests drive ``agent.managed_worker``'s gate helper with a
stub agent — the values the gate checks are the ones the client actually sends
with, so the assertions ride a real receipt in a real model_routing.db.
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


_CONFIG = {"model": {"default": "loop-model", "provider": "custom"},
           "platform_toolsets": {"cli": ["terminal"]}}


def _mint_and_link(home, conn, task_id):
    """The dispatcher's real mint path: resolve_task_route mints and CAS-links
    the receipt to the live run (RED fixture via the split's own machinery)."""
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


def _run_kanban_task(tmp_path, monkeypatch, params, *, calls, minted_receipt_id):
    """Drive run_task with stubbed session creation/resume/submit; returns
    (run_task's result, the trusted policies handed to session creation, the
    receipt id a concurrent re-link minted, if asked)."""
    import asyncio
    from gateway.session_contract import Principal
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    def create_local_session(authority, actor, session_params, *, trusted_policy=None,
                             trusted_secrets=None):
        holder.append(trusted_policy)
        return SimpleNamespace(session_id="owner-session", profile_id=str(tmp_path))

    from dataclasses import dataclass

    @dataclass
    class _Receipt:
        request_id: str
        session_id: str

    async def submit(actor, submission, **kwargs):
        calls.append(("submit", submission))
        return _Receipt(request_id=submission.request_id, session_id=submission.ref.session_id)

    async def resume(ref, params_):
        calls.append(("resume", ref))

    db = SimpleNamespace(get_session=lambda sid: None, db_path=str(tmp_path / "state.db"))
    authority = SimpleNamespace(profile_id=str(tmp_path), db=db, submit=submit)
    actor = Principal("owner", str(tmp_path), frozenset({"session:create"}), "transport")
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: _CONFIG)
    monkeypatch.setattr("gateway.session_local.create_local_session", create_local_session)

    holder = []
    relinked = {}

    def relink_between_build_and_admission(authority_, actor_, session_params, **kwargs):
        from agent.model_selection import select
        from agent.model_selection_store import persist_receipt
        # A concurrent dispatcher re-resolves this run and links a DIFFERENT
        # receipt (here: one minted for another task, then CAS-set onto the
        # same row/run — exactly what the admission predicate must catch).
        requirements = {
            "schema_version": 1, "role": "builder", "execution_kind": "kanban",
            "execution_id": "t_foreign_relink", "attempt_id": str(params["run_id"]),
            "slot_id": "", "task_class": "", "required_capabilities": [],
            "input_tokens": 1000, "reserve_tokens": 8192, "reasoning": "medium",
            "provenance": {"frozen_sha": "beefdead", "verified_by": "other",
                           "complete": True, "contributors": []},
        }
        relinked["receipt_id"] = persist_receipt(tmp_path, select(requirements, _policy(), {}, now=2000))
        with closing(connect(board="owned")) as conn:
            conn.execute("UPDATE tasks SET routing_receipt_id=? WHERE id=?",
                         (relinked["receipt_id"], params["task_id"]))
            conn.commit()
        holder.append(kwargs["trusted_policy"])
        return SimpleNamespace(session_id="owner-session", profile_id=str(tmp_path))

    if minted_receipt_id == "relink":
        monkeypatch.setattr("gateway.session_local.create_local_session",
                            relink_between_build_and_admission)
    from gateway.session_kanban import run_task
    return asyncio.run(run_task(connection, params)), holder, relinked


def test_admission_predicate_includes_receipt_id(kanban_env, monkeypatch):
    """TOCTOU (I7): the task's live receipt link changes between the fence's
    derivation and admission — same run id, different receipt — and the admission
    refuses with routing_blocked instead of admitting under a stale decision."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_state_runtime import RuntimeStoreError

    with closing(kbc.connect(board="owned")) as conn:
        tid = kb.create_task(conn, title="routed", assignee="default", workspace_kind="dir",
                             workspace_path=str(kanban_env / "ws"), routing_role="builder",
                             routing_mode="enforced",
                             routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192})
        (kanban_env / "ws").mkdir()
        task, _receipt_id = _mint_and_link(kanban_env, conn, tid)
    params = dict(board="owned", task_id=tid, run_id=task.current_run_id,
                  claim_lock=task.claim_lock)
    calls = []
    with pytest.raises(RuntimeStoreError) as excinfo:
        _run_kanban_task(kanban_env, monkeypatch, params, calls=calls, minted_receipt_id="relink")
    assert excinfo.value.reason == "routing_blocked"
    assert excinfo.value.detail == "stale_attempt"
    with closing(kbc.connect(board="owned")) as conn:
        admitted = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'",
            (tid,)).fetchone()[0]
    assert admitted == 0  # exactly one refusal, before owner_admitted
    assert calls == []


def test_receipted_session_admits_cleanly_when_link_intact(kanban_env, monkeypatch):
    """The happy path: the fence folds, admission passes, owner_admitted is written,
    and the submission carries the folded policy."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = kb.create_task(conn, title="routed", assignee="default", workspace_kind="dir",
                             workspace_path=str(kanban_env / "ws"), routing_role="builder",
                             routing_mode="enforced",
                             routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192})
        (kanban_env / "ws").mkdir()
        task, receipt_id = _mint_and_link(kanban_env, conn, tid)
    params = dict(board="owned", task_id=tid, run_id=task.current_run_id,
                  claim_lock=task.claim_lock)
    calls = []
    _result, holder, _relinked = _run_kanban_task(kanban_env, monkeypatch, params, calls=calls,
                                                  minted_receipt_id=None)
    assert any(kind == "submit" for kind, _ in calls)
    with closing(kbc.connect(board="owned")) as conn:
        admitted = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'",
            (tid,)).fetchone()[0]
    assert admitted == 1
    context = json.loads(holder[0].kanban_json)
    assert context["routing_receipt_id"] == receipt_id


def test_worker_side_attempt_binding_blocks_foreign_receipt(kanban_env):
    """A dispatch context carrying another task's valid receipt fails closed with
    stale_attempt before any request — the context is not a bearer token."""
    from agent.model_selection_types import RoutingBlocked
    from agent.managed_worker import _wire_kanban_routing_receipt

    receipt_id = _persist_gate_receipt(kanban_env)
    agent = _gate_agent(model="gpt-5", provider="openai")
    frame = {"policy": {"kanban_json": json.dumps(
        {"task_id": "t_other_task", "run_id": 1, "routing_receipt_id": receipt_id})}}
    with pytest.raises(RoutingBlocked, match="stale_attempt"):
        _wire_kanban_routing_receipt(agent, frame)


def test_pre_request1_gate_blocks_substituted_route(kanban_env, monkeypatch):
    """A construction-time substitution is caught before request 1 through the
    REAL construction path (_construct_agent): removing the gate call from the
    construction path fails this test. A matching construction wires both guard
    attributes (the per-request guard's inputs)."""
    from agent.managed_worker import _construct_agent
    from agent.model_selection_types import RoutingBlocked
    from hermes_cli import kanban_db_connect as kbc

    receipt_id = _persist_gate_receipt(kanban_env)
    with closing(kbc.connect(board="owned")) as conn:
        db_path = str(Path(conn.execute("SELECT file FROM pragma_database_list WHERE name='main'").fetchone()[0]))
    context = {"task_id": "t_gate_1", "run_id": 1, "routing_receipt_id": receipt_id, "db": db_path,
               "claim_lock": "fixture-lock"}
    frame = {"policy": {"kanban_json": json.dumps(context), "source": "kanban"}}
    policy = SimpleNamespace(kanban_json=frame["policy"]["kanban_json"])
    monkeypatch.setattr("gateway.session_worker_construct.worker_route", lambda *a, **k: {})

    substituted = _gate_agent(model="gpt-4o-legacy", provider="openai")
    constructed = []
    with pytest.raises(RoutingBlocked):  # 0 requests: nothing was sent
        _construct_agent(frame, lambda **kwargs: (constructed.append(kwargs), substituted)[1],
                         policy, api_key="k")
    assert constructed

    matching = _gate_agent(model="gpt-5", provider="openai", base_url="https://api.openai.com/v1")
    built = _construct_agent(frame, lambda **kwargs: matching, policy, api_key="k")
    assert built._managed_routing_receipt_id == receipt_id
    assert built._managed_routing_home == str(kanban_env)


def test_endpoint_untrusted_when_config_diverges(kanban_env):
    """A constructed endpoint the receipt does not name is blocked before any
    content is sent (endpoint_untrusted path via the pre-request-1 gate)."""
    from agent.model_selection_types import RoutingBlocked
    from agent.managed_worker import _wire_kanban_routing_receipt

    receipt_id = _persist_gate_receipt(kanban_env)
    frame = {"policy": {"kanban_json": json.dumps(
        {"task_id": "t_gate_1", "run_id": 1, "routing_receipt_id": receipt_id})}}
    diverged = _gate_agent(model="gpt-5", provider="openai", base_url="https://evil.example/v1")
    with pytest.raises(RoutingBlocked):
        _wire_kanban_routing_receipt(diverged, frame)


def test_fallback_disabled_on_receipted_frame(kanban_env, monkeypatch):
    """A receipted session never walks the fallback chain at construction: the
    worker resolves the primary route only (route_runtime receives fallback=False);
    an unrouted frame keeps the default."""
    captured = {}

    def fake_route_runtime(policy, frozen, model, key, *, fallback=True):
        captured["fallback"] = fallback
        return {"api_mode": None, "base_url": "https://api.openai.com/v1", "api_key": "k"}, None

    monkeypatch.setattr("gateway.session_worker_construct.route_runtime", fake_route_runtime)
    from gateway.session_worker_construct import worker_route

    def _policy_obj(kanban_context):
        from gateway.session_policy import LocalSessionPolicy
        return LocalSessionPolicy(source="kanban", platform="cli", cwd="/tmp", model="gpt-5",
                                  toolsets=("kanban",), config_json="{}", request_json="{}",
                                  terminal_json="{}", kanban_json=json.dumps(kanban_context))

    receipt_id = _persist_gate_receipt(kanban_env)
    routed = _policy_obj({"task_id": "t_gate_1", "run_id": 1, "routing_receipt_id": receipt_id})
    construct = {"api_key": "k", "fallback_model": [
        {"provider": "anthropic", "model": "claude-alternate"}]}
    worker_route(routed, construct)
    assert captured["fallback"] is False

    unrouted = _policy_obj({"task_id": "t_gate_1", "run_id": 1})
    worker_route(unrouted, construct)
    assert captured["fallback"] is True


def test_receipted_endpoint_none_skips_comparison(kanban_env):
    """The None rule (B3), worker side: a receipt that names no endpoint
    authorizes whatever the frozen config resolves — the endpoint comparison
    is skipped, and the gate passes on the config-resolved route."""
    from agent.managed_worker import _wire_kanban_routing_receipt

    receipt_id = _persist_gate_receipt(kanban_env, endpoint=None)
    frame = {"policy": {"kanban_json": json.dumps(
        {"task_id": "t_gate_1", "run_id": 1, "routing_receipt_id": receipt_id})}}
    agent = _gate_agent(model="gpt-5", provider="openai", base_url=None)
    _wire_kanban_routing_receipt(agent, frame)  # no block: endpoint comparison skipped
    assert agent._managed_routing_receipt_id == receipt_id


def test_fast_exit_submitter_classified_nonzero_exit(kanban_env):
    """The fence-refusal lifecycle (§4.1): a submitter that exits nonzero within
    milliseconds of spawn is classified ``nonzero_exit`` (→ ``crashed``, one
    ``consecutive_failures``), never misread as a clean or unknown exit."""
    import os

    from hermes_cli.kanban_db_dispatch import _classify_dead_worker_exit

    dead = _classify_dead_worker_exit(os.getpid(), "alice", exit_code=1, task_id="t_fence")
    assert dead.kind == "nonzero_exit"  # → crashed via _account_crashes, failures +1
    assert not dead.rate_limited and not dead.protocol_violation


def _gate_agent(model, provider, base_url=None):
    return SimpleNamespace(model=model, provider=provider, requested_provider="",
                           base_url=base_url, reasoning_config={"effort": "high"})


def _persist_gate_receipt(home, *, endpoint="https://api.openai.com/v1"):
    """A real receipt bound to task t_gate_1, attempt 1, with reasoning high."""
    from agent.model_selection import select
    from agent.model_selection_store import activate_policy, persist_receipt, publish_policy
    policy = _policy()
    policy["routes"][0]["endpoint"] = endpoint
    record = publish_policy(home, policy, approval_ref="operator:test")
    activate_policy(home, "kanban-default", record["revision"])
    requirements = {
        "schema_version": 1, "role": "builder", "execution_kind": "kanban",
        "execution_id": "t_gate_1", "attempt_id": "1", "slot_id": "",
        "task_class": "", "required_capabilities": [], "input_tokens": 1000,
        "reserve_tokens": 8192, "reasoning": "high",
        "provenance": {"frozen_sha": "deadbeef", "verified_by": "test",
                       "complete": True, "contributors": []},
    }
    return persist_receipt(home, select(requirements, policy, {}, now=1000))
