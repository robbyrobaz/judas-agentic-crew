"""Time budget must bound the cycle AFTER tool execution too (2026-09-16)."""
from __future__ import annotations

import time

from src.db.models import init_db
from src.research import agent_runner


def _resp(tool_calls):
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": f"c{i}", "type": "function",
                        "function": {"name": n, "arguments": "{}"}}
                       for i, n in enumerate(tool_calls)],
    }}], "usage": {"total_tokens": 10}}


def test_slow_tool_stops_cycle_at_time_budget(tmp_path, monkeypatch):
    db = str(tmp_path / "x.db")
    init_db(db)
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    calls = {"llm": 0}

    def fake_llm(**_kw):
        calls["llm"] += 1
        return _resp(["slow"])
    monkeypatch.setattr(agent_runner, "_call_llm", fake_llm)

    def slow():
        time.sleep(1.2)
        return {"ok": True}
    res = agent_runner.run_agent_loop(
        db_path=db, system_prompt="s", user_kickoff="k",
        tools={"slow": slow}, schemas=[], turn_budget=20, time_budget_s=1,
        team="researcher",
    )
    # one LLM turn, one slow tool, then the post-tool check ends the cycle —
    # NOT 20 turns of 1.2 s each.
    assert calls["llm"] == 1
    assert res.turns_used == 1
    assert res.error and res.error.startswith("time budget exhausted")
