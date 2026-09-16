"""modify_strategy_params runs the same validators as promote (2026-09-16),
and the registrar's insert_custom_strategy tool no longer raises TypeError."""
from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(path))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")
    from src.db.models import init_db
    init_db(path)
    return str(path)


def _seed(db, symbol="MGC", params=None):
    from src.strategy_registry import activate_seed_strategy
    return activate_seed_strategy(
        symbol=symbol, strategy_family="custom_5m",
        params=params or {"symbol": symbol, "strategy_family": "custom_5m",
                          "strategy_name": "x"},
        metrics={"seeded": True}, notes="t")


def test_modify_rejects_custom_engine_without_loadable_code(db):
    from src.research import pm_agent
    sid = _seed(db)
    tools = pm_agent._make_tools(db_path=db)
    out = tools["modify_strategy_params"](
        id=sid, rationale="r",
        new_params={"symbol": "MGC", "execution_engine": "custom",
                    "custom_strategy_id": 999999, "strategy_name": "x"})
    assert out["ok"] is False and "modify rejected" in out["error"]
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT state FROM active_strategies WHERE id=?",
                         (sid,)).fetchone()[0] == "active"
        assert c.execute("SELECT COUNT(*) FROM active_strategies").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM strategy_candidates").fetchone()[0] == 0


def test_modify_preserves_engine_and_code_link_on_partial_params(db):
    from src.research import pm_agent
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO custom_strategies (created_at_utc, name, symbol, code, rationale, backtest_metrics_json, active) "
                  "VALUES ('2026-09-01T00:00:00Z', 'cs', 'MGC', 'def evaluate(bars, params):\n    return None', 't', '{}', 1)")
        csid = c.execute("SELECT id FROM custom_strategies").fetchone()[0]
    sid = _seed(db, params={"symbol": "MGC", "strategy_family": "custom_5m",
                            "strategy_name": "x", "execution_engine": "custom",
                            "custom_strategy_id": csid, "target_r": 1.5})
    tools = pm_agent._make_tools(db_path=db)
    out = tools["modify_strategy_params"](
        id=sid, rationale="r", new_params={"symbol": "MGC", "target_r": 2.0})
    assert out["ok"] is True, out
    with sqlite3.connect(db) as c:
        p = json.loads(c.execute(
            "SELECT params_json FROM active_strategies WHERE state='active'").fetchone()[0])
    assert p["execution_engine"] == "custom" and p["custom_strategy_id"] == csid
    assert p["target_r"] == 2.0


def test_insert_custom_strategy_tool_inserts(db):
    from src.research import agent_tools
    out = agent_tools.insert_custom_strategy(
        symbol="MGC", strategy_family="custom_5m", strategy_name="fresh",
        params_json=json.dumps({"execution_engine": "judas_native", "target_r": 2.0}))
    assert out["ok"] is True, out
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT params_json, state FROM active_strategies WHERE id=?",
                        (out["strategy_id"],)).fetchone()
    assert row[1] == "active" and json.loads(row[0])["strategy_name"] == "fresh"
    bad = agent_tools.insert_custom_strategy(
        symbol="MBT", strategy_family="custom_5m", strategy_name="banned",
        params_json="{}")
    assert bad["ok"] is False and "banned" in bad["error"]
