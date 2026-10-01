"""Host-level concurrency accounting + review-lane fairness (OOF-30 review).

Three gaps found in review of the original memory-guard PR:

1. The standalone daemon path (``hermes kanban daemon --force`` /
   :func:`hermes_cli.kanban_db_dispatch.run_daemon`) never resolved
   ``kanban.max_in_progress`` at all — the one shipped entry point that
   could still fan out an entire backlog in a single tick.
2. ``max_in_progress`` was enforced per-board while the gateway dispatcher
   ticks every active board — N boards multiplied the host budget by N.
3. The ready loop consumed the entire shared spawn budget before the
   review loop ran, so a sustained ready backlog starved autonomous
   reviews indefinitely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


# ---------------------------------------------------------------------------
# 1. Standalone daemon resolves max_in_progress (P1a)
# ---------------------------------------------------------------------------


def test_run_daemon_resolves_and_passes_max_in_progress(
    kanban_home, monkeypatch,
):
    """The daemon tick must pass a resolved cap into dispatch_once.

    Regression guard for the OOF-30 review finding: ``run_daemon`` only
    forwarded ``max_spawn`` — with no explicit ``--max`` (the shipped
    systemd shape) nothing capped the tick even though the gateway and
    ``hermes kanban dispatch`` paths both resolved the memory-derived
    default.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    # No explicit config → the derived default must flow through.
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: None)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 3




def test_configured_max_in_progress_parsing(monkeypatch):
    import hermes_cli.config as cfgmod

    cases = [
        ({"kanban": {"max_in_progress": 4}}, 4),
        ({"kanban": {"max_in_progress": "5"}}, 5),
        ({"kanban": {"max_in_progress": 0}}, None),
        ({"kanban": {"max_in_progress": -2}}, None),
        ({"kanban": {"max_in_progress": "lots"}}, None),
        ({"kanban": {}}, None),
        ({}, None),
    ]
    for config, expected in cases:
        monkeypatch.setattr(
            cfgmod, "load_config_readonly", lambda c=config: c
        )
        assert kbd.configured_max_in_progress() == expected, config


# ---------------------------------------------------------------------------
# 2. max_in_progress counts running work on ALL boards (P1b)
# ---------------------------------------------------------------------------


def test_max_in_progress_counts_other_boards(
    kanban_home, all_assignees_spawnable,
):
    """Workers running on another board consume the same host budget."""
    kb.create_board("second")

    # Two workers already running on the second board.
    with kbc.connect(board="second") as conn:
        for title in ("busy-1", "busy-2"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Host budget (2) already consumed by the second board → nothing spawns.
    assert not spawns
    assert not res.spawned


def test_max_in_progress_partial_budget_across_boards(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("second")

    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # 1 running elsewhere + budget 2 → exactly one new spawn here.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_count_running_tasks_other_boards_fails_open(
    kanban_home, monkeypatch,
):
    """A broken board enumeration must not brick dispatch (returns 0)."""
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kbd.count_running_tasks_other_boards() == 0


def test_max_spawn_stays_per_board(kanban_home, all_assignees_spawnable):
    """``max_spawn`` keeps its historical per-board semantics."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1,
        )

    # The other board's worker does NOT count against max_spawn.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


# ---------------------------------------------------------------------------
# 3. Review lane cannot be starved by a sustained ready backlog (P2)
# ---------------------------------------------------------------------------


def _park_in_review(conn: sqlite3.Connection, title: str, assignee: str) -> str:
    tid = kb.create_task(conn, title=title, assignee=assignee)
    _set_task_status(conn, tid, "review")
    return tid


def test_review_lane_gets_reserved_slot_under_ready_backlog(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        review_id = _park_in_review(conn, "review-me", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    spawned_ids = [s[0] for s in res.spawned]
    # Budget 2: one ready + the reserved review slot — never 2×ready.
    assert len(spawned_ids) == 2
    assert review_id in spawned_ids


def _guard_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """Latest run ``rate_limited`` → ``check_respawn_guard`` returns a cooldown."""
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, "
            "started_at, ended_at) VALUES (?, 'reviewer', 'rate_limited', "
            "'rate_limited', ?, ?)",
            (review_id, now, now),
        )
    assert kbd.check_respawn_guard(conn, review_id, lane="review") == "rate_limit_cooldown"
    return {"max_in_progress": 1}


def _cap_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """``reviewer`` already has one running worker → the review row is per-profile capped."""
    busy_id = kb.create_task(conn, title="busy", assignee="reviewer")
    assert kb.claim_task(conn, busy_id) is not None
    return {"max_in_progress": 2, "max_in_progress_per_profile": 1}


@pytest.mark.parametrize("make_unspawnable", [_guard_review_row, _cap_review_row])
def test_unspawnable_review_does_not_reserve_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch, make_unspawnable,
):
    """A review card the review loop would refuse this tick (respawn guard,
    per-profile cap) must not consume the fairness reservation — otherwise the
    ready lane starves every tick while the reserved slot goes unused."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-unspawnable", "reviewer")
        caps = make_unspawnable(conn, review_id)
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), **caps)

    assert [task_id for task_id, *_ in res.spawned] == [ready_id]


def test_unguarded_review_reserves_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A dispatchable review card still receives the single shared slot."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-now", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
        )

    assert [task_id for task_id, *_ in res.spawned] == [review_id]


def test_review_reservation_released_when_no_review_work(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # No review work → ready lane keeps the full budget.
    assert len(res.spawned) == 2


def test_nonspawnable_review_does_not_tax_ready_budget(
    kanban_home, monkeypatch,
):
    """Review tasks parked for humans (no real profile) release the slot."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    # Only 'alice' is a real profile; the review assignee is a human lane.
    monkeypatch.setattr(
        profmod, "profile_exists", lambda name: name == "alice"
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2"):
            kb.create_task(conn, title=title, assignee="alice")
        _park_in_review(conn, "human-review", "some-human")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Human-lane review is not spawnable → no reservation, ready gets both.
    assert len(res.spawned) == 2


def test_review_budget_still_bounded_by_shared_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The reservation caps the ready lane; it grants review no extra slots."""
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-1", assignee="alice")
        for i in range(3):
            _park_in_review(conn, f"review-{i}", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Budget 2 total across both lanes, reservation notwithstanding.
    assert len(res.spawned) == 2


# ---------------------------------------------------------------------------
# Adaptive admission: spawn_allowance plumbing + admission-only sub-passes
# (plan rev2 §5.7, §5.2, §9). The allowance only LOWERS the budget the
# existing guards compute; defaults are byte-for-byte today's tick.
# ---------------------------------------------------------------------------


def _admission_fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


def _admission_row_counts(conn):
    return {
        "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        "task_events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
        "task_runs": conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
    }


def test_t15_allowance_two_spawns_exactly_two(
    kanban_home, all_assignees_spawnable,
):
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(10):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=2,
        )

    assert len(res.spawned) == 2
    assert res.admission_hold == 2


def test_t15_allowance_smaller_than_ceiling_wins(
    kanban_home, all_assignees_spawnable,
):
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=50, spawn_allowance=2,
        )

    assert len(res.spawned) == 2
    assert res.admission_hold == 2


def test_t15_allowance_zero_spawns_nothing(
    kanban_home, all_assignees_spawnable,
):
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=50, spawn_allowance=0,
        )

    assert not res.spawned
    assert res.admission_hold == 0


def test_t16_defaults_are_todays_tick(kanban_home, all_assignees_spawnable):
    """``spawn_allowance=None`` + ``admission_only=False`` behave exactly as today."""
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns), max_in_progress=3,
        )

    assert len(res.spawned) == 3
    assert res.admission_hold is None
    assert res.admission_reason is None


def test_t17_run_daemon_passes_no_allowance(kanban_home, monkeypatch):
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: None)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda res: stop.set())

    assert "spawn_allowance" not in captured
    assert "admission_only" not in captured


def test_t17_run_daemon_prints_static_note_when_mode_not_off(
    kanban_home, monkeypatch, capsys,
):
    """The deprecated daemon prints a note when adaptive admission is on."""
    from hermes_cli import kanban_admission as ka

    stop = threading.Event()
    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kw: kb.DispatchResult())
    monkeypatch.setattr(
        ka, "live_admission_settings",
        lambda: ka.parse_admission_settings(
            {"kanban": {"adaptive_admission": {"mode": "enforce"}}}),
    )
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda res: stop.set())

    out = capsys.readouterr().out
    assert "adaptive admission" in out
    assert "static" in out


def test_t18_host_allowance_shared_across_boards(
    kanban_home, all_assignees_spawnable,
):
    """One host allowance shared across boards: each tick gets its own 2."""
    kb.create_board("second")
    spawns: list = []
    with kbc.connect(board="second") as conn:
        for i in range(5):
            kb.create_task(conn, title=f"s{i}", assignee="alice")
        res_second = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=2,
        )
    with kbc.connect() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res_first = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=2,
        )

    assert len(res_second.spawned) == 2
    assert len(res_first.spawned) == 2
    assert len(spawns) == 4


def test_t18_failed_claim_does_not_consume_allowance(
    kanban_home, all_assignees_spawnable,
):
    """A row whose claim fails leaves the budget for the next rows."""
    spawns: list = []
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="claimed", assignee="alice")
        assert kb.claim_task(conn, first, ttl_seconds=300) is not None
        for i in range(2):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=2,
        )

    assert len(res.spawned) == 2


def test_t18_allowance_never_widens_ceiling(
    kanban_home, all_assignees_spawnable,
):
    """Host running on another board counts against the ceiling before the
    allowance applies: the allowance can only lower, never widen."""
    kb.create_board("second")
    spawns: list = []
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=2, spawn_allowance=2,
        )

    assert len(res.spawned) == 1


def _admission_review_row(conn, title="review-me"):
    tid = kb.create_task(conn, title=title, assignee="reviewer")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def test_t25_admission_only_skips_reclaim_and_checkpoint(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    reclaim_calls: list = []
    checkpoint_calls: list = []
    monkeypatch.setattr(kbd, "_run_reclaim_phase", lambda *a, **k: reclaim_calls.append(1))
    monkeypatch.setattr(
        kbd._kbc, "_maybe_checkpoint_wal", lambda *a, **k: checkpoint_calls.append(1))
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(2):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            admission_only=True, spawn_allowance=2,
        )

    assert reclaim_calls == []
    assert checkpoint_calls == []
    assert len(spawns) == 2


def test_t25_admission_only_skips_tick_hook_fires_worker_hook(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    from hermes_cli.plugins import get_plugin_manager

    mgr = get_plugin_manager()
    tick_events: list[dict] = []
    spawned_events: list[dict] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("on_kanban_dispatch_tick", []).append(
        lambda **kw: tick_events.append(kw))
    mgr._hooks.setdefault("on_kanban_worker_spawned", []).append(
        lambda **kw: spawned_events.append(kw))
    try:
        spawns: list = []
        with kbc.connect() as conn:
            kb.create_task(conn, title="r0", assignee="alice")
            kbd.dispatch_once(
                conn, spawn_fn=_admission_fake_spawn_factory(spawns),
                admission_only=True, spawn_allowance=1,
            )
    finally:
        mgr._hooks = saved

    assert tick_events == []
    assert len(spawned_events) == 1


def test_t25_admission_only_keeps_ceiling(
    kanban_home, all_assignees_spawnable,
):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=2, admission_only=True, spawn_allowance=5,
        )

    # 1 already running + ceiling 2 -> one more, allowance 5 notwithstanding.
    assert len(spawns) == 1


def test_t25_admission_only_keeps_memory_tiers(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kbd, "_system_memory_sample",
        lambda: {"mem_available_kib": 32 * 1024, "mem_total_kib": 1024 * 1024},
    )
    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="r0", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            admission_only=True, spawn_allowance=2,
        )

    assert not spawns
    assert res.memory_pressure == "critical"


def test_t25_admission_only_keeps_per_profile_cap(
    kanban_home, all_assignees_spawnable,
):
    spawns: list = []
    with kbc.connect() as conn:
        busy = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy) is not None
        for i in range(2):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress_per_profile=1, admission_only=True, spawn_allowance=2,
        )

    assert not spawns
    assert len(res.skipped_per_profile_capped) == 2


def test_t25_admission_only_keeps_review_reservation(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Allowance 1 with spawnable review work -> 0 ready + 1 review."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        review_id = _admission_review_row(conn)
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            admission_only=True, spawn_allowance=1,
        )

    assert [s[0] for s in res.spawned] == [review_id]


def test_t25_full_tick_review_reservation_with_allowance(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Full tick, allowance 2 + spawnable review -> 1 ready + 1 review."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        _admission_review_row(conn)
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=2,
        )

    assert len(res.spawned) == 2


def _admission_guard_row(conn):
    """Latest run rate_limited -> ``check_respawn_guard`` returns a cooldown."""
    tid = kb.create_task(conn, title="guarded", assignee="alice")
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, "
            "started_at, ended_at) VALUES (?, 'alice', 'rate_limited', "
            "'rate_limited', ?, ?)",
            (tid, now, now),
        )
    assert kbd.check_respawn_guard(conn, tid, lane="ready") == "rate_limit_cooldown"
    return tid


def test_t26_admission_only_guarded_row_no_event_written(
    kanban_home, all_assignees_spawnable,
):
    """A guarded row is skipped in a sub-pass WITHOUT the respawn_guarded
    event write, and leaves the row counts of tasks/events/runs unchanged."""
    spawns: list = []
    with kbc.connect() as conn:
        guarded = _admission_guard_row(conn)
        before = _admission_row_counts(conn)
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            admission_only=True, spawn_allowance=2,
        )
        after = _admission_row_counts(conn)

    assert after == before
    assert [g[0] for g in res.respawn_guarded] == [guarded]
    assert not spawns


def test_t26_full_tick_writes_exactly_one_guard_event(
    kanban_home, all_assignees_spawnable,
):
    spawns: list = []
    with kbc.connect() as conn:
        guarded = _admission_guard_row(conn)
        before = _admission_row_counts(conn)
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
        )
        after = _admission_row_counts(conn)

    assert after["task_events"] == before["task_events"] + 1
    assert [g[0] for g in res.respawn_guarded] == [guarded]


def test_t26_admission_only_still_applies_default_assignee(
    kanban_home, all_assignees_spawnable,
):
    """``_apply_default_assignee`` runs before the lane and stays in sub-passes."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unassigned")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            default_assignee="alice",
            admission_only=True, spawn_allowance=1,
        )

    assert [s[0] for s in res.spawned] == [tid]
    assert tid in res.auto_assigned_default


def _admission_pressure_sample(level: str) -> dict:
    total = 1024 * 1024  # KiB
    if level == "critical":
        return {"mem_available_kib": 32 * 1024, "mem_total_kib": total}
    if level == "elevated":
        return {"mem_available_kib": 100 * 1024, "mem_total_kib": total}
    return {"mem_available_kib": total // 2, "mem_total_kib": total}


def test_t19_critical_pressure_zero_with_allowance(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kbd, "_system_memory_sample", lambda: _admission_pressure_sample("critical")
    )
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=3,
        )

    assert not spawns
    assert res.memory_pressure == "critical"


def test_t19_elevated_pressure_one_with_allowance(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(
        kbd, "_system_memory_sample", lambda: _admission_pressure_sample("elevated")
    )
    spawns: list = []
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"r{i}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_admission_fake_spawn_factory(spawns),
            max_in_progress=100, spawn_allowance=3,
        )

    assert len(spawns) == 1
    assert res.memory_pressure == "elevated"


def test_t23_describe_suppression_includes_admission():
    res = kb.DispatchResult(admission_reason="cooldown")
    line = kbd.describe_suppression([res])
    assert "admission=cooldown" in line


def test_t23_describe_suppression_without_admission_unchanged():
    res = kb.DispatchResult()
    res.respawn_guarded.append(("t1", "blocker_auth"))
    line = kbd.describe_suppression([res])
    assert "admission=" not in line
    assert "blocker_auth=1" in line


def test_count_running_tasks_all_boards_none_on_error(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        kb.create_task(conn, title="r", assignee="alice")
    assert kbd.count_running_tasks_all_boards() == 0
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kbd.count_running_tasks_all_boards() is None


def test_count_running_tasks_all_boards_counts_every_board(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("second")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="busy1", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy2", assignee="alice")
        assert kb.claim_task(conn, tid) is not None
    assert kbd.count_running_tasks_all_boards() == 2
