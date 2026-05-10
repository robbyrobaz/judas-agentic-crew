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
