"""Cross-team regressions: palette isolation + delegation flow."""
from __future__ import annotations

from src.db.models import init_db
from src.research import (
    agent_tools, operator_agent, researcher_agent,
    trader_agent, registrar_agent,
)


# Action tools that must NOT appear on the Operator's palette.
_OPERATOR_FORBIDDEN = {
    "retire_strategy", "promote_candidate", "modify_strategy_params",
    "place_paper_order", "place_bracket_order", "cancel_order",
    "reactivate_demoted",
    "propose_candidate", "propose_custom_strategy", "retire_custom_strategy",
    "run_judas_threshold_sweep", "run_walk_forward", "run_custom_backtest",
    "web_search", "web_fetch", "fetch_youtube_transcript",
    "search_youtube_trading_videos",
    "read_file", "list_files", "read_research_artifact",
}


def test_operator_palette_has_no_action_tools(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=operator_agent.INCLUDE_TOOLS, operator_mode=True,
    )
    leaks = _OPERATOR_FORBIDDEN & set(tools)
    assert not leaks, f"Operator palette leaks action tools: {sorted(leaks)}"


def test_researcher_cannot_retire_or_trade(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=researcher_agent.INCLUDE_TOOLS, team="researcher",
    )
    forbidden = {"retire_strategy", "promote_candidate",
                 "place_bracket_order", "place_paper_order",
                 "cancel_order", "reactivate_demoted"}
    leaks = forbidden & set(tools)
    assert not leaks, f"Researcher leak: {leaks}"


def test_trader_cannot_propose_or_mutate_registry(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=trader_agent.INCLUDE_TOOLS, team="trader",
    )
    forbidden = {"retire_strategy", "promote_candidate",
                 "modify_strategy_params", "propose_candidate",
                 "propose_custom_strategy", "web_search",
                 "fetch_youtube_transcript", "run_judas_threshold_sweep"}
    leaks = forbidden & set(tools)
    assert not leaks


def test_registrar_cannot_trade_or_ingest(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    tools, _ = agent_tools.make_tools(
        db_path=db, include=registrar_agent.INCLUDE_TOOLS, team="registrar",
    )
    forbidden = {"place_bracket_order", "place_paper_order", "cancel_order",
                 "web_search", "web_fetch", "fetch_youtube_transcript",
                 "run_judas_threshold_sweep", "run_custom_backtest"}
    leaks = forbidden & set(tools)
    assert not leaks


def test_all_specialists_have_queue_tools(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    for include, team in (
        (researcher_agent.INCLUDE_TOOLS, "researcher"),
        (trader_agent.INCLUDE_TOOLS, "trader"),
        (registrar_agent.INCLUDE_TOOLS, "registrar"),
    ):
        tools, _ = agent_tools.make_tools(
            db_path=db, include=include, team=team,
        )
        for q in ("claim_task", "complete_task", "get_open_tasks"):
            assert q in tools, f"{team} missing {q}"


def test_search_youtube_trading_videos_filters_to_youtube(monkeypatch):
    """search_youtube_trading_videos must reject non-YouTube results."""
    class _FakeDDGS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def videos(self, query, max_results=5):
            return [
                {"title": "yt good", "content": "https://youtu.be/abcdefghijk",
                 "duration": "5m", "uploader": "X", "description": "snippet"},
                {"title": "vimeo bad", "content": "https://vimeo.com/123",
                 "duration": "5m"},
            ]
    monkeypatch.setattr(agent_tools, "search_youtube_trading_videos",
                        agent_tools.search_youtube_trading_videos)
    # Patch DDGS resolution.
    import sys, types
    fake_mod = types.ModuleType("ddgs")
    fake_mod.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake_mod)

    out = agent_tools.search_youtube_trading_videos(query="MGC liquidity sweep")
    assert out["ok"] is True
    urls = [r["url"] for r in out["results"]]
    assert all("youtu" in u for u in urls)
    assert any(r["video_id"] == "abcdefghijk" for r in out["results"])
