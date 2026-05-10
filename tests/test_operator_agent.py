"""Operator agent: delegations only, no direct actions."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.db.models import init_db
from src.research import operator_agent, pm_agent


def _enable(monkeypatch):
    monkeypatch.delenv("JUDAS_OPERATOR_AGENT_INHIBIT", raising=False)
    monkeypatch.delenv("JUDAS_PM_AGENT_INHIBIT", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "test")


def _scripted(steps):
    iterator = iter(steps)
    def fake(messages, tools, model, timeout_s):
        return next(iterator)
    return fake


def _llm_tool(name, args):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "x", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            }
        }]
    }


def _llm_text(text):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}]
    }


def test_no_api_key_returns_empty_delegations(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("JUDAS_OPERATOR_AGENT_INHIBIT", raising=False)
    # JUDAS_PM_AGENT_INHIBIT is the conftest default — leave set.
    db = str(tmp_path / "j.db")
    init_db(db)
    out = operator_agent.run_operator_decision(db_path=db)
    assert out.delegations == []
    assert out.fallback_used is True


def test_inhibit_env_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_OPERATOR_AGENT_INHIBIT", "1")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    init_db(db)
    out = operator_agent.run_operator_decision(db_path=db)
    assert out.delegations == []
    assert out.fallback_used is True


def test_delegate_to_researcher_enqueues_task(tmp_path, monkeypatch):
    _enable(monkeypatch)
    db = str(tmp_path / "j.db")
    init_db(db)

    monkeypatch.setattr(pm_agent, "_call_llm", _scripted([
        _llm_tool("delegate_to_researcher", {
            "topic": "MGC overnight sweep",
            "rationale": "leaderboard says MGC degrading",
            "urgency": "high",
        }),
        _llm_text("Delegated MGC research to Researcher."),
    ]))

    out = operator_agent.run_operator_decision(
        db_path=db, turn_budget=5, time_budget_s=60,
    )
    assert out.success is True
    assert len(out.delegations) == 1
    d = out.delegations[0]
    assert d.team == "researcher"
    assert d.urgency == "high"

    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT team, action, urgency, status FROM agent_tasks"
        ).fetchone()
    assert row == ("researcher", "research_topic", "high", "open")


def test_delegate_to_trader_enqueues_trade(tmp_path, monkeypatch):
    _enable(monkeypatch)
    db = str(tmp_path / "j.db")
    init_db(db)

    monkeypatch.setattr(pm_agent, "_call_llm", _scripted([
        _llm_tool("delegate_to_trader", {
            "symbol": "MGC", "side": "BUY", "qty": 1,
            "stop": 1900.0, "target": 1950.0,
            "rationale": "high conviction sweep",
        }),
        _llm_text("done"),
    ]))
    out = operator_agent.run_operator_decision(db_path=db, turn_budget=5)
    assert len(out.delegations) == 1
    assert out.delegations[0].team == "trader"

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM agent_tasks").fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["symbol"] == "MGC"
    assert payload["side"] == "BUY"


def test_operator_palette_excludes_action_tools(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    from src.research import agent_tools
    tools, schemas = agent_tools.make_tools(
        db_path=db,
        include=operator_agent.INCLUDE_TOOLS,
        operator_mode=True,
    )
    forbidden = {
        "retire_strategy", "promote_candidate", "modify_strategy_params",
        "place_paper_order", "place_bracket_order",
        "propose_candidate", "propose_custom_strategy",
        "web_search", "web_fetch", "fetch_youtube_transcript",
        "search_youtube_trading_videos",
        "read_file", "list_files", "read_research_artifact",
        "run_judas_threshold_sweep", "run_walk_forward", "run_custom_backtest",
        "cancel_order", "reactivate_demoted",
    }
    for f in forbidden:
        assert f not in tools, f"Operator must NOT have action tool: {f}"

    schema_names = {s["function"]["name"] for s in schemas}
    for f in forbidden:
        assert f not in schema_names

    # And it MUST have the four delegations.
    for d in ("delegate_to_researcher", "delegate_to_trader",
              "delegate_to_registrar", "delegate_to_coder"):
        assert d in tools, f"Operator missing delegation: {d}"
