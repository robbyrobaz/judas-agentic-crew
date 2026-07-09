"""Registrar deterministic queue regressions."""
from __future__ import annotations

import json
import sqlite3

from src.db.models import init_db
from src.research import agent_tools, registrar_agent


def _seed_active(db: str) -> int:
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            """INSERT INTO active_strategies
               (symbol, strategy_family, version, params_json, metrics_json,
                state, activated_at_utc)
               VALUES ('MBT', 'custom', 7,
                       '{"strategy_name":"mbt_csid_211","symbol":"MBT"}',
                       '{}', 'active', '2026-07-01T00:00:00Z')"""
        )
        conn.commit()
        return int(cur.lastrowid)


def test_registrar_executes_queued_retire_without_llm(tmp_path, monkeypatch):
    """Structured retire tasks land even when M3 is unavailable."""
    monkeypatch.delenv("JUDAS_REGISTRAR_AGENT_INHIBIT", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    db = str(tmp_path / "j.db")
    monkeypatch.setenv("JUDAS_DB_PATH", db)
    init_db(db)
    sid = _seed_active(db)

    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(
        team="registrar",
        action="retire_strategy",
        payload={"target_id": sid, "params": {}},
        rationale="MBT sleeve protection: max_consec_L >= 6 and negative P&L",
        urgency="high",
    )["task_id"]

    out = registrar_agent.run_registrar_decision(db_path=db, turn_budget=5, time_budget_s=60)

    assert out.success is True
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT status, result_json FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        active = conn.execute("SELECT state FROM active_strategies WHERE id=?", (sid,)).fetchone()
    assert task["status"] == "done"
    assert "demotion_id" in task["result_json"]
    assert active["state"] == "retired"


def test_registrar_executes_queued_eval_only_modify_without_llm(tmp_path, monkeypatch):
    """Patch-style eval_only modifies land without dropping runtime params."""
    monkeypatch.delenv("JUDAS_REGISTRAR_AGENT_INHIBIT", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    db = str(tmp_path / "j.db")
    monkeypatch.setenv("JUDAS_DB_PATH", db)
    init_db(db)
    params = {
        "symbol": "MBT",
        "strategy_name": "mbt_csid_211",
        "strategy_family": "custom",
        "execution_engine": "custom",
        "custom_strategy_id": 211,
        "timeframe": "5m",
        "qty": 1,
    }
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO active_strategies "
            "(symbol, strategy_family, version, params_json, metrics_json, state, activated_at_utc) "
            "VALUES ('MBT', 'custom', 7, ?, '{}', 'active', '2026-07-01T00:00:00Z')",
            (json.dumps(params),),
        )
        sid = int(cur.lastrowid)
        conn.commit()

    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(
        team="registrar",
        action="modify_strategy_params",
        payload={"target_id": sid, "params": {"eval_only_no_orders": True}},
        rationale="halt MBT orders while retaining evaluation/audit",
        urgency="high",
    )["task_id"]

    out = registrar_agent.run_registrar_decision(db_path=db, turn_budget=5, time_budget_s=60)

    assert out.success is True
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        old_row = conn.execute("SELECT state FROM active_strategies WHERE id=?", (sid,)).fetchone()
        new_row = conn.execute(
            "SELECT version, params_json FROM active_strategies "
            "WHERE symbol='MBT' AND state='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    new_params = json.loads(new_row["params_json"])
    assert task["status"] == "done"
    assert old_row["state"] == "retired"
    assert int(new_row["version"]) == 8
    assert new_params["eval_only_no_orders"] is True
    assert new_params["execution_engine"] == "custom"
    assert new_params["custom_strategy_id"] == 211
    assert new_params["timeframe"] == "5m"
