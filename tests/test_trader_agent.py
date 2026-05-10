"""Trader specialist regressions."""
from __future__ import annotations

import json

from src.db.models import init_db
from src.research import trader_agent, pm_agent, agent_tools


def test_inhibit_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_TRADER_AGENT_INHIBIT", "1")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    init_db(db)
    out = trader_agent.run_trader_decision(db_path=db)
    assert out.fallback_used is True


def test_palette_excludes_ingestion_and_registry(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=trader_agent.INCLUDE_TOOLS, team="trader",
    )
    forbidden = {
        "retire_strategy", "promote_candidate", "modify_strategy_params",
        "propose_candidate", "propose_custom_strategy",
        "web_search", "web_fetch", "fetch_youtube_transcript",
        "read_file", "list_files",
        "run_judas_threshold_sweep", "run_walk_forward",
    }
    for f in forbidden:
        assert f not in tools
    for ok in ("place_bracket_order", "cancel_order", "claim_task",
               "complete_task", "get_open_tasks", "get_fills",
               "get_open_positions"):
        assert ok in tools


def test_claim_then_place_bracket(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDAS_TRADER_AGENT_INHIBIT", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    init_db(db)

    # Pre-queue a trade task.
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="trader", action="place_trade",
              payload={"symbol": "MGC", "side": "BUY", "qty": 1,
                       "stop": 1900.0, "target": 1950.0},
              rationale="r")["task_id"]

    # Mock the broker seam.
    def fake_place(**kw):
        return {"parent_order_id": 1, "tp_order_id": 2, "sl_order_id": 3,
                "local_symbol": "MGC", "status": "Submitted"}
    monkeypatch.setattr(pm_agent, "_place_bracket", fake_place)

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
                "function": {"name": "place_bracket_order",
                             "arguments": json.dumps({
                                 "symbol": "MGC", "side": "BUY", "quantity": 1,
                                 "stop_price": 1900.0, "target_price": 1950.0,
                                 "rationale": "queued",
                             })},
            }],
        }}]},
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c", "type": "function",
                "function": {"name": "complete_task",
                             "arguments": json.dumps({
                                 "task_id": tid,
                                 "result": {"signal_id": 1},
                                 "status": "done",
                             })},
            }],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "Done."}}]},
    ])
    monkeypatch.setattr(pm_agent, "_call_llm",
                        lambda **kw: next(iter_responses))

    out = trader_agent.run_trader_decision(
        db_path=db, turn_budget=10, time_budget_s=60,
    )
    assert out.success is True
    actions = [a.action for a in out.actions_taken]
    assert "place_bracket_order" in actions
    assert "complete_task" in actions
