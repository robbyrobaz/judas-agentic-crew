"""Researcher specialist regressions."""
from __future__ import annotations

import json
import sqlite3

from src.db.models import init_db
from src.research import researcher_agent, pm_agent, agent_tools


def _llm_text(t):
    return {"choices": [{"message": {"role": "assistant", "content": t}}]}


def test_inhibit_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_RESEARCHER_AGENT_INHIBIT", "1")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    init_db(db)
    out = researcher_agent.run_researcher_decision(db_path=db)
    assert out.fallback_used is True
    assert out.actions_taken == []


def test_no_api_key_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("JUDAS_RESEARCHER_AGENT_INHIBIT", raising=False)
    db = str(tmp_path / "j.db")
    init_db(db)
    out = researcher_agent.run_researcher_decision(db_path=db)
    assert out.fallback_used is True


def test_palette_excludes_action_tools(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=researcher_agent.INCLUDE_TOOLS, team="researcher",
    )
    forbidden = {
        "retire_strategy", "promote_candidate", "modify_strategy_params",
        "place_paper_order", "place_bracket_order", "cancel_order",
        "reactivate_demoted",
    }
    for f in forbidden:
        assert f not in tools, f"Researcher must NOT have: {f}"
    # And the proposal tools MUST be present.
    for ok in ("propose_candidate", "propose_custom_strategy",
               "run_judas_threshold_sweep", "claim_task", "complete_task"):
        assert ok in tools


def test_loop_invokes_propose_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDAS_RESEARCHER_AGENT_INHIBIT", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    db = str(tmp_path / "j.db")
    init_db(db)

    iter_responses = iter([
        {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "x", "type": "function",
                "function": {
                    "name": "propose_candidate",
                    "arguments": json.dumps({
                        "symbol": "MGC",
                        "strategy_family": "judas_1h",
                        "params": {"strategy_name": "judas_test"},
                        "rationale": "test pulse",
                    }),
                },
            }],
        }}]},
        _llm_text("Proposed one MGC candidate."),
    ])
    monkeypatch.setattr(pm_agent, "_call_llm",
                        lambda **kw: next(iter_responses))

    out = researcher_agent.run_researcher_decision(
        db_path=db, turn_budget=5, time_budget_s=60,
    )
    assert out.success is True
    assert any(a.action == "propose_candidate" for a in out.actions_taken)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates WHERE rationale='test pulse'"
        ).fetchone()[0] == 1
