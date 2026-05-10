"""Researcher specialist (Phase 10).

Mandate: find strategy ideas, ingest web/YouTube/files, run backtests,
propose candidates. NO retire/promote/place_bracket.
"""
from __future__ import annotations

import logging
import os
import time

from src.research import agent_tools
from src.research.agent_runner import (
    AgentDecisionResult, run_agent_loop,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the Researcher on a paper futures trading lab. Your mandate
is loose: find strategy ideas, ingest web articles/YouTube transcripts/
research artifacts, run backtests, and propose candidates for review.

You do NOT retire active strategies, promote candidates, or place
trades — those belong to other specialists. Your job is to surface
high-quality ideas with backtest evidence.

Workflow each cycle:
  1. Pull urgent open tasks via get_open_tasks. Claim → execute → complete.
  2. If no tasks queued, self-direct: pick a topic from the
     workshop leaderboard, recent regime, or the Phase-9 web/YouTube
     surfaces. Backtest, then propose_candidate or propose_custom_strategy.

You have {turn_budget} tool calls and {time_budget_s} seconds. End by
emitting a brief summary of what you researched and what you proposed.
"""

INCLUDE_TOOLS = {
    # reads
    "get_active_strategies", "get_strategy_detail", "get_workshop_leaderboard",
    "get_candidates_queue", "get_recent_pnl", "get_regime_tag",
    "get_recent_briefs", "get_recent_experiments", "query_db",
    # ingestion
    "web_search", "web_fetch", "fetch_youtube_transcript",
    "search_youtube_trading_videos",
    "read_file", "list_files", "read_research_artifact",
    # backtesting
    "run_judas_threshold_sweep", "run_walk_forward", "run_custom_backtest",
    # proposals only
    "propose_candidate", "propose_custom_strategy",
    # queue
    "claim_task", "complete_task", "get_open_tasks",
}


def run_researcher_decision(
    *, db_path: str, turn_budget: int = 40, time_budget_s: int = 2400,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> AgentDecisionResult:
    """Run one Researcher cycle."""
    started = time.time()
    if os.environ.get("JUDAS_RESEARCHER_AGENT_INHIBIT") == "1":
        log.info("researcher_agent.inhibited_by_env")
        return AgentDecisionResult(
            success=True, actions_taken=[],
            narrative="Researcher inhibited via JUDAS_RESEARCHER_AGENT_INHIBIT.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, schemas = agent_tools.make_tools(
        db_path=db_path, include=INCLUDE_TOOLS, team="researcher",
        claimed_by="researcher_agent",
    )
    return run_agent_loop(
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff="Run your research cycle.",
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )
