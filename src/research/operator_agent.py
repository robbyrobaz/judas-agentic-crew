"""Operator agent (Phase 10) — manager that delegates.

Tools = delegations + reads. NOT actions. The four ``delegate_to_*``
tools enqueue rows on ``agent_tasks`` for the specialists to consume.
The Operator does not retire/promote/place_bracket/ingest directly —
those belong to the Researcher, Trader, Registrar, and Coder.

Tests monkeypatch ``src.research.pm_agent._call_llm`` (shared seam).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.research import agent_tools
from src.research.agent_runner import (
    AgentDecisionResult, run_agent_loop,
)

log = logging.getLogger(__name__)

_ET_TZ = ZoneInfo("America/New_York")


SYSTEM_PROMPT = """\
You are the manager of a paper futures trading lab with a $5,000 IBKR
paper account. Your only job is to make as much money as possible.

You don't do the work yourself — you have a team:
  - Researcher: finds strategy ideas, ingests web/YouTube/files,
    runs backtests, proposes candidates.
  - Trader: places trades safely through the deterministic broker,
    manages brackets, reports fills.
  - Registrar: atomic retire/promote/modify on the strategy registry.
  - Coder: fixes bugs in the system code.

Your tools are DELEGATIONS, not actions:
  delegate_to_researcher(topic, urgency, rationale)
  delegate_to_trader(symbol, side, qty, stop, target, rationale)
  delegate_to_registrar(action, target_id, params, reason)
  delegate_to_coder(symptom, context)

Plus reads so you know what to delegate:
  get_active_strategies, get_recent_pnl, get_recent_briefs,
  get_outstanding_delegations, get_recent_trades,
  get_candidates_queue, get_workshop_leaderboard, query_db

Be decisive. Delegate aggressively. The specialists handle execution
safely — your job is the strategic decisions: what to research, what
to trade, what to retire, what to fix.

You have {turn_budget} tool calls and {time_budget_s} seconds. End
by emitting a brief summary of what you delegated and why.
"""


# Operator's palette: delegations + reads only. Explicitly NOT action tools.
INCLUDE_TOOLS = {
    # delegations
    "delegate_to_researcher", "delegate_to_trader",
    "delegate_to_registrar", "delegate_to_coder",
    # reads
    "get_active_strategies", "get_recent_pnl", "get_recent_briefs",
    "get_outstanding_delegations", "get_recent_trades",
    "get_candidates_queue", "get_workshop_leaderboard", "query_db",
    "get_strategy_detail", "get_recent_experiments", "get_open_positions",
    "get_regime_tag",
}


@dataclass
class Delegation:
    team: str
    action: str
    payload: dict
    rationale: str
    urgency: str
    task_id: int | None


@dataclass
class OperatorDecisionResult:
    success: bool
    delegations: list[Delegation]
    actions_taken: list  # = delegations cast to action shape (back-compat)
    narrative: str
    turns_used: int
    elapsed_s: float
    fallback_used: bool
    raw_messages: list
    error: str | None = None


def run_operator_decision(
    *, db_path: str, turn_budget: int = 20, time_budget_s: int = 900,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> OperatorDecisionResult:
    """Run one Operator (manager) cycle."""
    started = time.time()
    if os.environ.get("JUDAS_OPERATOR_AGENT_INHIBIT") == "1" \
            or os.environ.get("JUDAS_PM_AGENT_INHIBIT") == "1":
        log.info("operator_agent.inhibited_by_env")
        return OperatorDecisionResult(
            success=True, delegations=[], actions_taken=[],
            narrative="Operator inhibited via env (test/safety mode); no delegations.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, schemas = agent_tools.make_tools(
        db_path=db_path, include=INCLUDE_TOOLS, operator_mode=True,
    )

    try:
        date_et = datetime.now(_ET_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        date_et = datetime.now(timezone.utc).isoformat()

    base = run_agent_loop(
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff=f"It's {date_et}. Manage the lab.",
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )

    delegations: list[Delegation] = []
    for a in base.actions_taken:
        if not a.action.startswith("delegate_to_"):
            continue
        team_map = {
            "delegate_to_researcher": "researcher",
            "delegate_to_trader": "trader",
            "delegate_to_registrar": "registrar",
            "delegate_to_coder": "coder",
        }
        team = team_map.get(a.action, "?")
        payload = a.payload or {}
        tid = None
        if isinstance(a.tool_result, dict):
            tid = a.tool_result.get("task_id")
        delegations.append(Delegation(
            team=team,
            action=a.action,
            payload=payload,
            rationale=a.rationale or str(payload.get("rationale") or ""),
            urgency=str(payload.get("urgency") or "normal"),
            task_id=tid,
        ))

    return OperatorDecisionResult(
        success=base.success,
        delegations=delegations,
        actions_taken=base.actions_taken,
        narrative=base.narrative,
        turns_used=base.turns_used,
        elapsed_s=base.elapsed_s,
        fallback_used=base.fallback_used,
        raw_messages=base.raw_messages,
        error=base.error,
    )
