"""agent_tasks queue: enqueue → claim → complete."""
from __future__ import annotations

import sqlite3

from src.db.models import init_db
from src.research import agent_tools


def _setup(tmp_path):
    db = str(tmp_path / "x.db")
    init_db(db)
    return db


def test_enqueue_inserts_open_row(tmp_path):
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    out = enq(team="researcher", action="research_topic",
              payload={"topic": "MGC sweep"}, rationale="rebuild MGC pipeline")
    assert out["ok"] is True
    tid = out["task_id"]
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT team, status, action FROM agent_tasks WHERE id=?",
                        (tid,)).fetchone()
    assert row == ("researcher", "open", "research_topic")


def test_claim_then_complete_writes_correct_rows(tmp_path):
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="trader", action="place_trade",
              payload={"symbol": "MGC"}, rationale="r")["task_id"]

    claim = agent_tools.make_claim_task(db_path=db, team="trader",
                                        claimed_by="trader_agent")
    res = claim(task_id=tid)
    assert res["ok"] is True
    assert res["task"]["action"] == "place_trade"

    # second claim must fail.
    again = claim(task_id=tid)
    assert again["ok"] is False

    comp = agent_tools.make_complete_task(db_path=db)
    out = comp(task_id=tid, result={"signal_id": 7}, status="done")
    assert out["ok"] is True

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT status, claimed_by, result_json FROM agent_tasks WHERE id=?",
                        (tid,)).fetchone()
    assert row["status"] == "done"
    assert row["claimed_by"] == "trader_agent"
    assert "signal_id" in row["result_json"]


def test_claim_rejects_wrong_team(tmp_path):
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="researcher", action="x",
              payload={}, rationale="r")["task_id"]

    claim = agent_tools.make_claim_task(db_path=db, team="trader",
                                        claimed_by="trader_agent")
    res = claim(task_id=tid)
    assert res["ok"] is False
    assert "researcher" in res["error"]


def test_get_open_tasks_orders_by_urgency(tmp_path):
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    enq(team="researcher", action="a", payload={}, rationale="r", urgency="low")
    enq(team="researcher", action="b", payload={}, rationale="r", urgency="high")
    enq(team="researcher", action="c", payload={}, rationale="r", urgency="normal")

    get = agent_tools.make_get_open_tasks(db_path=db, team="researcher")
    rows = get(limit=10)
    assert [r["action"] for r in rows] == ["b", "c", "a"]


def test_enqueue_dedupes_identical_open_task(tmp_path):
    """2026-09-16: 85 open registrar tasks, 24 on two already-retired ids —
    the operator kept re-dispatching the same retire. Same team+action+target
    while a prior row is open/claimed must return that row, not insert."""
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    a = enq(team="registrar", action="retire_strategy",
            payload={"target_id": 4597, "params": {}}, rationale="r1")
    b = enq(team="registrar", action="retire_strategy",
            payload={"target_id": 4597, "params": {}}, rationale="r2 different text")
    assert a["ok"] and b["ok"]
    assert b["task_id"] == a["task_id"]
    assert b.get("deduped") is True
    # different action on same target is NOT a dup
    c = enq(team="registrar", action="modify_strategy_params",
            payload={"target_id": 4597, "params": {"x": 1}}, rationale="r")
    assert c["task_id"] != a["task_id"]
    # once the first is terminal, a fresh one may be queued
    comp = agent_tools.make_complete_task(db_path=db)
    comp(task_id=a["task_id"], result={}, status="done")
    d = enq(team="registrar", action="retire_strategy",
            payload={"target_id": 4597, "params": {}}, rationale="r3")
    assert d["task_id"] != a["task_id"] and not d.get("deduped")
    with sqlite3.connect(db) as c_:
        n = c_.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
    assert n == 3


def test_abandon_tasks_bulk(tmp_path):
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    ids = [enq(team="registrar", action="retire_strategy",
               payload={"target_id": i}, rationale="r")["task_id"] for i in (1, 2, 3)]
    comp = agent_tools.make_complete_task(db_path=db)
    comp(task_id=ids[2], result={}, status="done")
    ab = agent_tools.make_abandon_tasks(db_path=db, actor="registrar_agent")
    out = ab(task_ids=ids + [9999], reason="superseded")
    assert out["ok"] is True
    assert out["abandoned"] == ids[:2]
    assert out["skipped"][ids[2]] == "already done"
    assert out["skipped"][9999] == "not found"
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT id, status FROM agent_tasks ORDER BY id").fetchall()
    assert rows == [(ids[0], "abandoned"), (ids[1], "abandoned"), (ids[2], "done")]
    assert ab(task_ids=[], reason="x")["ok"] is False


def test_get_open_tasks_default_window_is_50(tmp_path):
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    for i in range(60):
        enq(team="registrar", action="retire_strategy",
            payload={"target_id": i}, rationale="r")
    get = agent_tools.make_get_open_tasks(db_path=db, team="registrar")
    assert len(get()) == 50
    assert len(get(limit=200)) == 60


def test_release_unfinished_claims_reopens_then_abandons(tmp_path):
    """Claimed-but-unfinished work goes back to 'open' at cycle end (was: sat
    'claimed' 24h then reaped to 'abandoned' — every researcher task Sep 3-16)."""
    db = _setup(tmp_path)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="researcher", action="research_topic",
              payload={"topic": "x"}, rationale="r")["task_id"]
    claim = agent_tools.make_claim_task(db_path=db, team="researcher",
                                        claimed_by="researcher_agent")
    for n in range(1, 4):
        assert claim(task_id=tid)["ok"] is True
        out = agent_tools.release_unfinished_claims(
            db_path=db, team="researcher", claimed_by="researcher_agent")
        assert out["reopened"] == [tid], n
        with sqlite3.connect(db) as c:
            st, cb, rj = c.execute(
                "SELECT status, claimed_by, result_json FROM agent_tasks WHERE id=?",
                (tid,)).fetchone()
        assert st == "open" and cb is None and f'"requeues": {n}' in rj
    # 4th unfinished claim → abandoned
    assert claim(task_id=tid)["ok"] is True
    out = agent_tools.release_unfinished_claims(
        db_path=db, team="researcher", claimed_by="researcher_agent")
    assert out["abandoned"] == [tid]
    # another agent's claims are untouched
    tid2 = enq(team="researcher", action="research_topic",
               payload={"topic": "y"}, rationale="r")["task_id"]
    claim2 = agent_tools.make_claim_task(db_path=db, team="researcher",
                                         claimed_by="someone_else")
    claim2(task_id=tid2)
    agent_tools.release_unfinished_claims(
        db_path=db, team="researcher", claimed_by="researcher_agent")
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT status FROM agent_tasks WHERE id=?",
                         (tid2,)).fetchone()[0] == "claimed"
