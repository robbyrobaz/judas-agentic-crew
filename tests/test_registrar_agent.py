"""Registrar specialist regressions."""
from __future__ import annotations

import json
import sqlite3

from src.db.models import init_db
from src.research import registrar_agent, pm_agent, agent_tools


def test_inhibit_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_REGISTRAR_AGENT_INHIBIT", "1")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    init_db(db)
    out = registrar_agent.run_registrar_decision(db_path=db)
    assert out.fallback_used is True


def test_palette_excludes_ingestion_and_trading(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=registrar_agent.INCLUDE_TOOLS, team="registrar",
    )
    forbidden = {
        "place_paper_order", "place_bracket_order", "cancel_order",
        "web_search", "web_fetch", "fetch_youtube_transcript",
        "run_judas_threshold_sweep", "run_walk_forward",
        "propose_candidate", "propose_custom_strategy",
    }
    for f in forbidden:
        assert f not in tools
    for ok in ("retire_strategy", "promote_candidate",
               "modify_strategy_params", "reactivate_demoted",
               "claim_task", "complete_task"):
        assert ok in tools


def _seed_active(db):
    with sqlite3.connect(db) as c:
        cur = c.execute(
            """INSERT INTO active_strategies
               (symbol, strategy_family, version, params_json, metrics_json,
                state, activated_at_utc)
               VALUES ('MGC', 'judas_1h', 1,
                       '{"strategy_name": "judas_test", "symbol": "MGC"}',
                       '{}', 'active', '2026-05-01T00:00:00Z')"""
        )
        c.commit()
        return int(cur.lastrowid)


def test_claim_then_retire(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDAS_REGISTRAR_AGENT_INHIBIT", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    monkeypatch.setenv("JUDAS_DB_PATH", db)
    init_db(db)
    sid = _seed_active(db)

    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="registrar", action="retire_strategy",
              payload={"target_id": sid, "params": {}},
              rationale="cleanup")["task_id"]

    iter_responses = iter([
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "a", "type": "function",
                "function": {"name": "claim_task",
                             "arguments": json.dumps({"task_id": tid})},
            }],
        }}]},
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "b", "type": "function",
                "function": {"name": "retire_strategy",
                             "arguments": json.dumps({"id": sid,
                                                      "reason": "cleanup"})},
            }],
        }}]},
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c", "type": "function",
                "function": {"name": "complete_task",
                             "arguments": json.dumps({
                                 "task_id": tid,
                                 "result": {"retired": sid},
                             })},
            }],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    monkeypatch.setattr(pm_agent, "_call_llm",
                        lambda **kw: next(iter_responses))

    out = registrar_agent.run_registrar_decision(
        db_path=db, turn_budget=10, time_budget_s=60,
    )
    actions = [a.action for a in out.actions_taken]
    assert "retire_strategy" in actions
    assert "complete_task" in actions

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT state FROM active_strategies WHERE id=?",
                        (sid,)).fetchone()
    assert row["state"] == "retired"
