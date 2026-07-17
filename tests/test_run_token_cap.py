"""Per-run token cap (2026-07-17).

With unlimited turns the ONLY stop condition was MiniMax's 429 — the operator
flow made 1,056 LLM calls in 47 minutes and consumed the entire 5h quota window
in one cycle, then the autofix coder repeated the pattern. The cap ends a run
cleanly once its cumulative recorded tokens cross JUDAS_RUN_TOKEN_CAP, letting
the next timer cycle continue with fresh context instead of burning the account.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _tool_call_response(tokens: int):
    """A response that always asks for another tool call (would loop forever)."""
    return {
        "usage": {"total_tokens": tokens},
        "choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "noop", "arguments": "{}"},
            }],
        }}],
    }


def test_run_token_cap_stops_infinite_loop(monkeypatch):
    import src.research.agent_runner as ar

    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("JUDAS_RUN_TOKEN_CAP", "1000000")
    calls = {"n": 0}

    def fake_llm(**kwargs):
        calls["n"] += 1
        return _tool_call_response(400_000)  # 3 calls > 1M cap

    monkeypatch.setattr(ar, "_call_llm", fake_llm)
    db = tempfile.mktemp(suffix=".db")
    result = ar.run_agent_loop(
        team="researcher", db_path=db,
        system_prompt="test", user_kickoff="go",
        tools={"noop": lambda **k: {"ok": True}},
        schemas=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        turn_budget=0, time_budget_s=0,  # unlimited — the cap must stop it
    )
    assert result.success
    assert calls["n"] == 3, f"expected 3 calls to cross 1M cap, got {calls['n']}"
    assert "token cap" in result.narrative or result.turns_used == 3


def test_usage_recorded_for_every_call(monkeypatch):
    import src.research.agent_runner as ar

    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("JUDAS_RUN_TOKEN_CAP", "1000000")
    monkeypatch.setattr(ar, "_call_llm", lambda **k: _tool_call_response(600_000))
    db = tempfile.mktemp(suffix=".db")
    ar.run_agent_loop(
        team="researcher", db_path=db,
        system_prompt="test", user_kickoff="go",
        tools={"noop": lambda **k: {"ok": True}},
        schemas=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        turn_budget=0, time_budget_s=0,
    )
    assert ar.daily_tokens_used(db_path=db) == 1_200_000  # both calls metered


def test_pm_agent_respects_daily_budget(monkeypatch):
    """The operator/pm path must skip cleanly when the daily budget is spent —
    it burned invisibly (unmetered, ungated) before 2026-07-17."""
    import src.research.agent_runner as ar
    import src.research.pm_agent as pm

    monkeypatch.delenv("JUDAS_PM_AGENT_INHIBIT", raising=False)  # conftest sets it
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("JUDAS_DAILY_TOKEN_BUDGET", "1000")
    db = tempfile.mktemp(suffix=".db")
    ar._record_llm_usage(db_path=db, tokens=5000)  # already over budget

    called = {"n": 0}
    def fake_llm(**kwargs):
        called["n"] += 1
        return _tool_call_response(100)
    monkeypatch.setattr(pm, "_call_llm", fake_llm)

    result = pm.run_pm_decision(db_path=db)
    assert result.fallback_used
    assert called["n"] == 0, "LLM must not be called once daily budget is spent"
    assert "budget" in result.narrative
