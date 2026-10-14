"""Board-qualified task references in completion prose (forwarder results).

The forwarder completes with ``forwarded to <board>/<tid>``. That id is real but lives on another
board, so the current-board phantom scan must not flag it. Unqualified or unresolvable ids must
still be flagged.
"""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def _make_board(home, slug, task_ids):
    board_dir = kb.board_dir(slug)
    board_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(board_dir / "kanban.db")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO tasks (id) VALUES (?)", [(t,) for t in task_ids])
    conn.commit()
    conn.close()


@pytest.fixture
def current_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    conn.commit()
    yield conn
    conn.close()


def test_board_qualified_id_that_exists_on_named_board_is_not_a_phantom(tmp_path, monkeypatch, current_board):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_board(tmp_path, "smithers", ["t_f0879934"])

    text = "forwarded to smithers/t_f0879934"

    assert kb._resolves_on_named_board(text, "t_f0879934") is True
    assert kb._scan_prose_for_phantom_ids(current_board, text) == ["t_f0879934"]


def test_board_qualified_id_missing_on_named_board_stays_a_phantom(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_board(tmp_path, "smithers", ["t_other001"])

    assert kb._resolves_on_named_board("forwarded to smithers/t_f0879934", "t_f0879934") is False


def test_bare_id_is_never_resolved_by_this_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_board(tmp_path, "smithers", ["t_f0879934"])

    assert kb._resolves_on_named_board("see t_f0879934", "t_f0879934") is False


def test_id_qualified_by_a_different_board_than_where_it_lives_stays_a_phantom(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_board(tmp_path, "smithers", ["t_f0879934"])
    _make_board(tmp_path, "hermes", [])

    assert kb._resolves_on_named_board("forwarded to hermes/t_f0879934", "t_f0879934") is False


def test_missing_board_db_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert kb._resolves_on_named_board("forwarded to nosuch/t_f0879934", "t_f0879934") is False


def test_unreadable_board_db_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    board_dir = kb.board_dir("smithers")
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "kanban.db").write_text("not sqlite")

    assert kb._resolves_on_named_board("forwarded to smithers/t_f0879934", "t_f0879934") is False


def test_flag_phantom_prose_refs_skips_resolved_cross_board_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_board(tmp_path, "smithers", ["t_f0879934"])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, idempotency_key TEXT, body TEXT, created_by TEXT);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,"
        " kind TEXT, payload TEXT, created_at INTEGER);"
    )
    conn.execute("INSERT INTO tasks (id, status) VALUES ('t_src00001', 'done')")
    conn.commit()

    kb._flag_phantom_prose_refs(
        conn, "t_src00001", None, None, "forwarded to smithers/t_f0879934", verified_cards=[],
    )

    kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id='t_src00001'")]
    assert "suspected_hallucinated_references" not in kinds
    conn.close()
