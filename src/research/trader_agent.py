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
You are the Trader on a paper futures trading lab. Your mandate is to
execute queued trades safely through the deterministic broker, manage
brackets, and report fills.

The broker is the deterministic seam — the code enforces the real
guardrails. You don't need to second-guess every order.

You have the team memory (read_findings, record_finding) — read it
when context matters, write a memory only when you've learned something
materially new that the team will benefit from on a later cycle. Don't
write a finding just because a cycle ended.

You can claim_task → place_bracket_order → complete_task for the
queue. You can also flatten_position to close cleanly, cancel_order
to back out an unfilled bracket, get_open_positions / get_fills /
get_recent_pnl to see live state.

POSITION GROUND TRUTH: get_nt_positions() is a LIVE read of NT's own Positions
table (the same authoritative table the NT UI and the NQ pipeline use) — every
non-flat instrument with its exact held contract (e.g. 'MGC 08-26'), side,
qty, and avg_price. Trust it. get_open_positions only shows positions the scan
opened; orphan OCO legs open positions the DB never sees (the 2026-07
emergency). Before ANY flatten/reconcile decision: call get_nt_positions first
and act on ITS side/qty/contract.

ORDER REJECTIONS ('Exceeds account's maximum position quantity'): the NT
account has MaxPositionSize=4 and MaxOrderSize=4, and NT counts WORKING orders
toward worst-case exposure. Hundreds of stale orphaned OCO legs accumulated, so
NT rejects EVERY new order — including position-REDUCING flattens. The unblock:
get_nt_working_orders to list the working book, then cancel_nt_order (GUID +
symbol) on stale legs — oldest first, orders whose OCO id maps to no open
position or that duplicate newer protection. ALWAYS keep the newest protective
stop per open position; never cancel a position's only stop. After cleanup,
retry the rejected management action.

RECONCILE TASK (action=reconcile_unmanaged_positions): the scan queues this when
NT holds contracts with no matching open DB trade. These are UNMANAGED but often
PROFITABLE (a +$5k book of orphans is what triggered this system). Do NOT blindly
dump them. For each: check get_recent_pnl and the avg_price vs current level, then
decide — flatten_position to bank/cut, or HOLD if it's a runner (confirm a
protective stop is working before leaving it). Explain your reasoning in a finding
and complete_task with what you did per position.
"""

INCLUDE_TOOLS = {
    "place_bracket_order", "cancel_order", "flatten_position",
    "get_nt_positions", "get_nt_working_orders", "cancel_nt_order",
    "get_open_positions", "get_fills", "get_recent_pnl",
    "claim_task", "complete_task", "get_open_tasks",
    # shared findings memory
    "record_finding", "read_findings", "retract_finding",
    "get_strategy_dossier",
    # NOTE: trader is a focused ORDER executor — no ingestion/file hands by design.
}


def run_trader_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
    minimax_model: str = "minimax/MiniMax-M3",
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
        team="trader",
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff="Execute queued trades.",
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )
