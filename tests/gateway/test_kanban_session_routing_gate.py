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
from typing import Optional

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


def _run_kanban_task(tmp_path, monkeypatch, params, *, calls, minted_receipt_id,
                     saved_policy=None):
    """Drive run_task with stubbed session creation/resume/submit; returns
    (run_task's result, the trusted policies handed to session creation, the
    receipt id a concurrent re-link minted, if asked). With ``saved_policy``
    (the committed policy dict the owner store holds for this attempt) the
    saved-claim replay branch runs instead: the attempt's session is restored,
    not created."""
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

    db = SimpleNamespace(get_session=lambda sid: ({"id": sid} if saved_policy is not None else None),
                         db_path=str(tmp_path / "state.db"))
    authority = SimpleNamespace(profile_id=str(tmp_path), db=db, submit=submit)
    actor = Principal("owner", str(tmp_path), frozenset({"session:create"}), "transport")
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: _CONFIG)
    monkeypatch.setattr("gateway.session_local.create_local_session", create_local_session)

    holder = []
    relinked = {}

    if saved_policy is not None:
        # The saved-claim replay: the owner store holds this attempt's committed
        # policy, so run_task restores the session instead of creating it.
        monkeypatch.setattr("hermes_state_local.local_receipt",
                            lambda db_, sid: {"policy": saved_policy})

        def restore_local_session(authority_, sid):
            calls.append(("restore", sid))
            return SimpleNamespace(session_id="owner-session", profile_id=str(tmp_path))

        monkeypatch.setattr("gateway.session_local_recovery.restore_local_session",
                            restore_local_session)
        monkeypatch.setattr("hermes_state_runtime.find_session_admission", lambda *a, **k: None)

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

    _tid, receipt_id = _gate_task_and_receipt(kanban_env)
    agent = _gate_agent(model="gpt-5", provider="openai")
    frame = {"policy": {"kanban_json": json.dumps(
        {"task_id": "t_other_task", "run_id": 1, "routing_receipt_id": receipt_id,
         "db": _board_db_path()})}}
    with pytest.raises(RoutingBlocked, match="stale_attempt"):
        _wire_kanban_routing_receipt(agent, frame)


def test_pre_request1_gate_blocks_substituted_route(kanban_env, monkeypatch):
    """A construction-time substitution is caught before request 1 through the
    REAL construction path (_construct_agent): removing the gate call from the
    construction path fails this test. A matching construction wires both guard
    attributes (the per-request guard's inputs)."""
    from agent.managed_worker import _construct_agent
    from agent.model_selection_types import RoutingBlocked

    tid, receipt_id = _gate_task_and_receipt(kanban_env)
    context = {"task_id": tid, "run_id": 1, "routing_receipt_id": receipt_id,
               "db": _board_db_path(), "claim_lock": "fixture-lock"}
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
    content is sent, with the kanban boundary's own detail — endpoint_untrusted,
    minted at the gate (the shared guard's provider/model/reasoning halves are
    unchanged for delegation and MoA)."""
    from agent.model_selection_types import RoutingBlocked
    from agent.managed_worker import _wire_kanban_routing_receipt

    tid, receipt_id = _gate_task_and_receipt(kanban_env)
    diverged = _gate_agent(model="gpt-5", provider="openai", base_url="https://evil.example/v1")
    with pytest.raises(RoutingBlocked) as excinfo:
        _wire_kanban_routing_receipt(diverged, _gate_frame(tid, receipt_id))
    assert excinfo.value.reason == "endpoint_untrusted"
    assert getattr(diverged, "_managed_routing_receipt_id", None) is None  # nothing wired


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

    tid, receipt_id = _gate_task_and_receipt(kanban_env)
    construct = {"api_key": "k", "fallback_model": [
        {"provider": "anthropic", "model": "claude-alternate"}]}
    routed = _gate_policy_obj({"task_id": tid, "run_id": 1, "routing_receipt_id": receipt_id})
    worker_route(routed, construct)
    assert captured["fallback"] is False

    unrouted = _gate_policy_obj({"task_id": tid, "run_id": 1})
    worker_route(unrouted, construct)
    assert captured["fallback"] is True


def test_receipted_endpoint_none_skips_comparison(kanban_env):
    """The None rule (B3), worker side: a receipt that names no endpoint
    authorizes whatever the frozen config resolves — the endpoint comparison
    is skipped, and the gate passes on the config-resolved route."""
    from agent.managed_worker import _wire_kanban_routing_receipt

    tid, receipt_id = _gate_task_and_receipt(kanban_env, endpoint=None)
    agent = _gate_agent(model="gpt-5", provider="openai", base_url=None)
    _wire_kanban_routing_receipt(agent, _gate_frame(tid, receipt_id))  # no block: comparison skipped
    assert agent._managed_routing_receipt_id == receipt_id


def test_saved_claim_recheck_reads_current_row(kanban_env, monkeypatch):
    """I6: the saved-claim re-check re-reads the LIVE task row at replay admission.
    A re-link while the run id stays the same (a foreign receipt now on the row,
    still routed enforced) blocks the replay with routing_blocked stale_attempt —
    one crash-class refusal, never an admission under a stale decision."""
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    params, tid, _receipt_id = _routed_task_with_receipt(kanban_env)
    saved_policy = _first_admission(kanban_env, monkeypatch, params)
    # A concurrent re-link between first admission and the replay: a foreign
    # receipt minted for another task now sits on the live row.
    with closing(kbc.connect(board="owned")) as conn:
        foreign = _foreign_receipt(kanban_env, "t_foreign_relink", str(params["run_id"]))
        conn.execute("UPDATE tasks SET routing_receipt_id=? WHERE id=?", (foreign, tid))
        conn.commit()
    replay_calls = []
    with pytest.raises(RuntimeStoreError) as excinfo:
        _run_kanban_task(kanban_env, monkeypatch, params, calls=replay_calls,
                         minted_receipt_id=None, saved_policy=saved_policy)
    assert excinfo.value.reason == "routing_blocked"
    assert excinfo.value.detail == "stale_attempt"
    # refused before resume/submit (the restore call itself is the harness's stub)
    assert not any(kind in ("submit", "resume") for kind, _ in replay_calls)
    with closing(kbc.connect(board="owned")) as conn:
        admitted = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'",
            (tid,)).fetchone()[0]
    assert admitted == 1  # only the first admission's marker


def test_derouted_replay_runs_unrouted(kanban_env, monkeypatch):
    """A task de-routed between first admission and the saved-claim replay is
    admitted unrouted and the worker gate genuinely enforces nothing: the stale
    receipt id frozen in the restored context unwires at construction (the
    carrier's rollback semantics) instead of gating the session under a dead
    receipt. Without the gate's live row read, the stale receipt keeps enforcing."""
    from agent.managed_worker import _wire_kanban_routing_receipt
    from gateway.session_policy import restore_policy
    from hermes_cli import kanban_db_connect as kbc

    params, tid, receipt_id = _routed_task_with_receipt(kanban_env)
    saved_policy = _first_admission(kanban_env, monkeypatch, params)
    with closing(kbc.connect(board="owned")) as conn:
        conn.execute("UPDATE tasks SET routing_role=NULL, routing_mode=NULL WHERE id=?", (tid,))
        conn.commit()
    replay_calls = []
    _run_kanban_task(kanban_env, monkeypatch, params, calls=replay_calls,
                     minted_receipt_id=None, saved_policy=saved_policy)
    assert any(kind == "submit" for kind, _ in replay_calls)  # admitted, no block

    # The restored context still freezes the receipt id; the worker's construction-time
    # gate re-reads the live (de-routed) row and unwires — the session runs unrouted.
    context = json.loads(restore_policy(saved_policy).kanban_json or "{}")
    assert context["routing_receipt_id"] == receipt_id
    agent = _gate_agent(model="gpt-5", provider="openai")
    _wire_kanban_routing_receipt(agent, {"policy": {"kanban_json": json.dumps(context)}})
    assert getattr(agent, "_managed_routing_receipt_id", None) is None
    assert getattr(agent, "_managed_routing_home", None) is None


def test_restored_session_agent_carries_guard_attributes(kanban_env, monkeypatch):
    """B4: the saved-claim replay's agent carries both per-request guard
    attributes — the restored frame wires the same receipt the fence verified at
    first admission, so every later request re-checks against it."""
    from agent.managed_worker import _construct_agent
    from gateway.session_policy import restore_policy

    params, _tid, receipt_id = _routed_task_with_receipt(kanban_env)
    saved_policy = _first_admission(kanban_env, monkeypatch, params)
    replay_calls = []
    _run_kanban_task(kanban_env, monkeypatch, params, calls=replay_calls,
                     minted_receipt_id=None, saved_policy=saved_policy)
    assert any(kind == "submit" for kind, _ in replay_calls)

    context = json.loads(restore_policy(saved_policy).kanban_json or "{}")
    frame = {"policy": {"kanban_json": json.dumps(context), "source": "kanban"}}
    policy = SimpleNamespace(kanban_json=frame["policy"]["kanban_json"])
    monkeypatch.setattr("gateway.session_worker_construct.worker_route", lambda *a, **k: {})
    agent = _gate_agent(model="gpt-5", provider="openai", base_url="https://api.openai.com/v1",
                        effort="medium")
    built = _construct_agent(frame, lambda **kwargs: agent, policy, api_key="k")
    assert built._managed_routing_receipt_id == receipt_id
    assert built._managed_routing_home == str(kanban_env)


def test_fallback_disabled_on_restored_session(kanban_env, monkeypatch):
    """B5+B4: a saved-claim replay does not regain fallback walking — the restored
    frame still carries the receipt, so worker_route resolves the primary only."""
    captured = {}

    def fake_route_runtime(policy, frozen, model, key, *, fallback=True):
        captured["fallback"] = fallback
        return {"api_mode": None, "base_url": "https://api.openai.com/v1", "api_key": "k"}, None

    monkeypatch.setattr("gateway.session_worker_construct.route_runtime", fake_route_runtime)
    from gateway.session_policy import restore_policy
    from gateway.session_worker_construct import worker_route

    params, _tid, receipt_id = _routed_task_with_receipt(kanban_env)
    saved_policy = _first_admission(kanban_env, monkeypatch, params)
    replay_calls = []
    _run_kanban_task(kanban_env, monkeypatch, params, calls=replay_calls,
                     minted_receipt_id=None, saved_policy=saved_policy)

    restored = restore_policy(saved_policy)
    assert json.loads(restored.kanban_json or "{}")["routing_receipt_id"] == receipt_id
    construct = {"api_key": "k", "fallback_model": [
        {"provider": "anthropic", "model": "claude-alternate"}]}
    worker_route(restored, construct)
    assert captured["fallback"] is False


def test_fallback_never_reached_on_receipted_session(kanban_env, monkeypatch):
    """B5, construction-time, through the REAL route_runtime: a receipted session
    with a fallback configured never walks the chain — the fallback walker runs
    exactly 0 times (no fallback route is ever resolved, so no request can reach
    it), and a failing primary fails the construction instead of activating the
    fallback route."""
    import hermes_cli.runtime_provider as rp

    walker_calls, primary_calls = [], []

    def walker(*a, **k):
        walker_calls.append(a)
        return ({"api_key": "k", "base_url": "https://fallback.example/v1", "api_mode": None},
                {"provider": "anthropic", "model": "claude-alternate"})

    def primary(*a, **k):
        primary_calls.append(a)
        return {"api_key": "k", "base_url": "https://api.openai.com/v1", "api_mode": None}

    monkeypatch.setattr(rp, "resolve_runtime_with_fallback", walker)
    monkeypatch.setattr(rp, "resolve_runtime_provider", primary)
    from gateway.session_worker_construct import worker_route

    tid, receipt_id = _gate_task_and_receipt(kanban_env)
    construct = {"api_key": "k", "fallback_model": [
        {"provider": "anthropic", "model": "claude-alternate"}]}
    routed = _gate_policy_obj({"task_id": tid, "run_id": 1, "routing_receipt_id": receipt_id})
    merged = worker_route(routed, construct)
    assert merged["base_url"] == "https://api.openai.com/v1"  # the receipted primary
    assert len(primary_calls) == 1
    assert walker_calls == []  # hard count: exactly 0 walks to the fallback route

    def failing(*a, **k):
        raise RuntimeError("primary auth failed")

    monkeypatch.setattr(rp, "resolve_runtime_provider", failing)
    with pytest.raises(RuntimeError):  # the failing primary fails the construction
        worker_route(routed, construct)
    assert walker_calls == []  # still exactly 0: the fallback was never activated


def test_midturn_fallback_activation_breaks_before_next_request(kanban_env):
    """B5, agent-level chain: a mid-turn fallback activation swaps the live route
    (provider+model together), and the per-request guard the loop runs before
    EVERY request reports the divergence — the turn breaks before the next
    request is built, so the unreceipted fallback route receives exactly 0
    requests."""
    from agent.managed_route_guard import enforce_managed_route_per_request
    from agent.managed_worker import _wire_kanban_routing_receipt

    tid, receipt_id = _gate_task_and_receipt(kanban_env)
    frame = _gate_frame(tid, receipt_id)
    agent = _gate_agent(model="gpt-5", provider="openai", base_url="https://api.openai.com/v1")
    _wire_kanban_routing_receipt(agent, frame)  # request 1's gate passes; guard wired
    requests = []

    def next_request():
        # The provider call the loop would make with the live (swapped) client.
        requests.append((agent.provider, agent.model, agent.base_url))

    # A fallback activation switches provider+model together (#139155): the live
    # agent now points at the unreceipted fallback route.
    agent.provider, agent.model, agent.base_url = ("anthropic", "claude-alternate",
                                                   "https://api.anthropic.com")
    reason = None
    for _ in range(2):  # later iterations of the same turn
        reason = enforce_managed_route_per_request(agent)
        if reason is not None:
            break  # the loop's fail-closed break — before the request is built
        next_request()
    assert reason is not None
    assert requests == []  # hard count: 0 requests reached the unreceipted route


def test_fast_exit_submitter_classified_nonzero_exit(kanban_env):
    """The fence-refusal lifecycle (§4.1): a submitter that exits nonzero within
    milliseconds of spawn is classified ``nonzero_exit`` (→ ``crashed``, one
    ``consecutive_failures``), never misread as a clean or unknown exit."""
    import os

    from hermes_cli.kanban_db_dispatch import _classify_dead_worker_exit

    dead = _classify_dead_worker_exit(os.getpid(), "alice", exit_code=1, task_id="t_fence")
    assert dead.kind == "nonzero_exit"  # → crashed via _account_crashes, failures +1
    assert not dead.rate_limited and not dead.protocol_violation


def _gate_agent(model, provider, base_url=None, effort="high"):
    return SimpleNamespace(model=model, provider=provider, requested_provider="",
                           base_url=base_url, reasoning_config={"effort": effort})


def _board_db_path():
    from hermes_cli import kanban_db_connect as kbc
    with closing(kbc.connect(board="owned")) as conn:
        return str(Path(conn.execute("SELECT file FROM pragma_database_list WHERE name='main'")
                        .fetchone()[0]))


def _gate_task_and_receipt(home, *, endpoint: Optional[str] = "https://api.openai.com/v1"):
    """A real board task row (routed enforced), a real receipt bound to it, and the
    row linked to the receipt the way the dispatcher CAS-links: the live row state
    the worker gate re-reads at construction time."""
    from agent.model_selection import select
    from agent.model_selection_store import activate_policy, persist_receipt, publish_policy
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with closing(kbc.connect(board="owned")) as conn:
        tid = kb.create_task(conn, title="gate", assignee="default", workspace_kind="dir",
                             workspace_path=str(home / "ws"), routing_role="builder",
                             routing_mode="enforced")
        (home / "ws").mkdir(exist_ok=True)
    policy = _policy()
    policy["routes"][0]["endpoint"] = endpoint
    record = publish_policy(home, policy, approval_ref="operator:test")
    activate_policy(home, "kanban-default", record["revision"])
    requirements = {
        "schema_version": 1, "role": "builder", "execution_kind": "kanban",
        "execution_id": tid, "attempt_id": "1", "slot_id": "",
        "task_class": "", "required_capabilities": [], "input_tokens": 1000,
        "reserve_tokens": 8192, "reasoning": "high",
        "provenance": {"frozen_sha": "deadbeef", "verified_by": "test",
                       "complete": True, "contributors": []},
    }
    receipt_id = persist_receipt(home, select(requirements, policy, {}, now=1000))
    with closing(kbc.connect(board="owned")) as conn:
        conn.execute("UPDATE tasks SET routing_receipt_id=? WHERE id=?", (receipt_id, tid))
        conn.commit()
    return tid, receipt_id


def _gate_frame(tid, receipt_id):
    return {"policy": {"kanban_json": json.dumps(
        {"task_id": tid, "run_id": 1, "routing_receipt_id": receipt_id,
         "db": _board_db_path()})}}


def _gate_policy_obj(kanban_context):
    from gateway.session_policy import LocalSessionPolicy
    return LocalSessionPolicy(source="kanban", platform="cli", cwd="/tmp", model="gpt-5",
                              toolsets=("kanban",), config_json="{}", request_json="{}",
                              terminal_json="{}", kanban_json=json.dumps(kanban_context))


def _routed_task_with_receipt(home):
    """A dispatcher-minted receipt (resolve_task_route) on a live claimed run —
    the first admission's fixture."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with closing(kbc.connect(board="owned")) as conn:
        tid = kb.create_task(conn, title="routed", assignee="default", workspace_kind="dir",
                             workspace_path=str(home / "ws"), routing_role="builder",
                             routing_mode="enforced",
                             routing_requirements={"input_tokens": 1000, "reserve_tokens": 8192})
        (home / "ws").mkdir(exist_ok=True)
        task, receipt_id = _mint_and_link(home, conn, tid)
        assert task is not None and receipt_id is not None
    params = dict(board="owned", task_id=tid, run_id=task.current_run_id,
                  claim_lock=task.claim_lock)
    return params, tid, receipt_id


def _first_admission(home, monkeypatch, params):
    """Run run_task once through the fresh path; return the committed policy dict
    the owner store holds for this attempt (the saved-claim replay's source)."""
    from dataclasses import asdict
    calls = []
    _result, holder, _relinked = _run_kanban_task(home, monkeypatch, params, calls=calls,
                                                  minted_receipt_id=None)
    assert any(kind == "submit" for kind, _ in calls)
    return asdict(holder[0])


def _foreign_receipt(home, execution_id, attempt_id):
    from agent.model_selection import select
    from agent.model_selection_store import persist_receipt
    requirements = {
        "schema_version": 1, "role": "builder", "execution_kind": "kanban",
        "execution_id": execution_id, "attempt_id": attempt_id, "slot_id": "",
        "task_class": "", "required_capabilities": [], "input_tokens": 1000,
        "reserve_tokens": 8192, "reasoning": "medium",
        "provenance": {"frozen_sha": "beefdead", "verified_by": "other",
                       "complete": True, "contributors": []},
    }
    return persist_receipt(home, select(requirements, _policy(), {}, now=2000))
