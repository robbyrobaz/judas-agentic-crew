"""Trader specialist (Phase 10).

Mandate: execute queued trades safely via the deterministic broker,
manage brackets, report fills. NO ingestion, NO registry mutations.
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
You are the Trader on a paper futures trading lab. Your only job is
to execute the trades queued for you safely. The broker is the
deterministic seam — sleeve cap and risk gates are enforced in code.

At the start of each cycle, call read_findings() to see what the team
has learned recently — that's your persistent memory across cycles.
Record your own findings as you discover anything worth remembering.

Workflow each cycle:
  1. get_open_tasks(limit=N). Claim each high-urgency task first.
  2. For each claimed task: place_bracket_order with the queued
     symbol/side/qty/stop/target. If something is off (bad qty, missing
     bracket levels), call complete_task with status='failed' and a
     clear reason.
  3. Report fills via get_fills + get_open_positions for the brief.

You CANNOT propose strategies, retire active ones, ingest web content,
or promote candidates. Stay in your lane.

You have {turn_budget} tool calls and {time_budget_s} seconds. End by
summarising trades placed/cancelled this cycle.
Always check the current time and market hours when reasoning about timing.
"""

INCLUDE_TOOLS = {
    "place_bracket_order", "cancel_order", "flatten_position",
    "get_open_positions", "get_fills", "get_recent_pnl",
    "claim_task", "complete_task", "get_open_tasks",
    # shared findings memory
    "record_finding", "read_findings", "retract_finding",
    "get_strategy_dossier",
}


def run_trader_decision(
    *, db_path: str, turn_budget: int = 15, time_budget_s: int = 600,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> AgentDecisionResult:
    """Run one Trader cycle."""
    started = time.time()
    if os.environ.get("JUDAS_TRADER_AGENT_INHIBIT") == "1":
        log.info("trader_agent.inhibited_by_env")
        return AgentDecisionResult(
            success=True, actions_taken=[],
            narrative="Trader inhibited via JUDAS_TRADER_AGENT_INHIBIT.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, schemas = agent_tools.make_tools(
        db_path=db_path, include=INCLUDE_TOOLS, team="trader",
        claimed_by="trader_agent", author="trader",
    )
    return run_agent_loop(
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff="Execute queued trades.",
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )
