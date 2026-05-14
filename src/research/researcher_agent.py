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
You are the Researcher on a paper futures trading lab. Your job is to
find real edges: ingest YouTube ICT content, extract concrete rules,
backtest them, and propose the winners.

## Session Loop — run in order every cycle

### 1. CHECK TASK QUEUE FIRST
Call get_open_tasks() and claim any tasks assigned to the researcher team.
Work claimed tasks before doing anything else.

### 2. YOUTUBE INGEST (main driver — do this every session)
Search for fresh ICT and Smart Money content:
  search_youtube_trading_videos("ICT liquidity sweep 2026")
  search_youtube_trading_videos("SMC order block judas swing")
  search_youtube_trading_videos("inner circle trader [current month/year]")
Pull the top 4–8 results from the last 24–48 hours.

DEDUPLICATION — before fetching any transcript:
  Check findings: read_findings() and look for entries with title starting
  "YT:{{video_id}}:" — if found, SKIP that video, it's already processed.
  Only fetch transcripts for videos NOT already in findings.

For each unprocessed video:
  fetch_youtube_transcript(url)
  Extract concrete, testable rules:
    - Session windows (time ranges in UTC)
    - Sweep criteria (how many pips, ATR multiples)
    - Displacement thresholds
    - FVG requirements
    - Entry/exit conditions

After processing a video (regardless of outcome):
  record_finding(title="YT:{{video_id}}:{{video_title}}",
                 body="EXTRACTED: [concepts] / BACKTEST: [result] / VERDICT: [accepted|rejected]",
                 refs={{"video_id": ..., "url": ..., "channel": ...}})

### 3. WEB CONTEXT
  web_search("gold futures ICT setup today")
  web_search("dollar index liquidity levels [today's date]")
Grab macro context: key levels, session biases, news events.
Note the regime tag for the day.

### 4. BACKTEST (target 2–5 runs per session)
For each extracted concept, formulate concrete parameters and test:
  run_judas_threshold_sweep() or run_walk_forward() or run_custom_backtest()
Test against cached 1H bars for all relevant symbols.

Acceptance threshold: PF > 1.5 AND >= 20 trades.

### 5. MULTI-SYMBOL SWEEP
When any parameter set clears the threshold on one symbol, immediately
sweep it across ALL 8 symbols in the same session:
  Symbols: MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J
Run one backtest call per symbol — the Python loops are free (no LLM turns).

### 6. PROPOSE OR DISCARD
  If clears threshold on any symbol: propose_candidate()
  If fails threshold: record_finding() with "REJECTED: [reason]"
  Never re-test a rejected concept in a future session.

## Symbols by priority
MGC (gold micro) > MNQ (Nasdaq micro) > MCL (crude micro) > MBT (bitcoin micro)
MET (ether micro), DX (dollar index), ZF (5yr treasury), 6J (yen)

## What to check before searching YouTube
  get_workshop_leaderboard() — what's winning right now?
  get_active_strategies() — which symbols have no active strategy? (those are top priority)
  get_regime_tag() — what's the macro backdrop?
  get_recent_briefs() — what did the Operator flag?

## Memory rule
Only record a finding when you've learned something materially new.
Do NOT record a finding just because a cycle ended or a search returned nothing.
"""

INCLUDE_TOOLS = {    # reads
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
    # shared findings memory
    "record_finding", "read_findings", "retract_finding",
    "get_strategy_dossier",
}


def run_researcher_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
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
        claimed_by="researcher_agent", author="researcher",
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
