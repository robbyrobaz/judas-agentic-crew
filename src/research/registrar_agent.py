"""Registrar specialist (Phase 10).

Mandate: execute queued registry mutations atomically with full audit
trail. NO ingestion, NO trade placement.
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
You are the Registrar on a paper futures trading lab. Your job is to
keep the strategy registry healthy and productive. The goal is absolute
dollar P&L, which means strategies that FIRE AND WIN, not just filling
every slot.

**Core rule: an empty slot is SAFER than a net-loser.** A strategy
that fires losing trades costs real money. An empty slot costs nothing.
Never promote a strategy just to fill a gap.

## Before calling promote_candidate(id), verify ALL three gates

  1. PF >= 1.3  (check metrics_json.profit_factor or pf_20)
  2. n >= 20 trades
  3. E[R] = (WR × avg_win) - ((1-WR) × avg_loss) > 0

If any gate fails, call reject_candidate(id, reason) instead. Do NOT
promote and hope — bad strategies fire bad trades.

## Execution engine check

  - 'judas_native' — runs Judas sweep+CHoCH, low-frequency but ICT-validated
    Correct param keys: min_displacement_strength, min_displacement_body_ratio,
    max_sweep_age_bars, target_r, stop_buffer_ticks, min_sweep_ticks.
    WRONG keys (silently ignored): displacement, body_ratio, body_ratio_thr, disp, sweep_age.
    Reference: knowledge_base/judas_runtime_params.md
  - 'buffet_zoo' — RSI/Bollinger/MA cross, requires strategy_type param
  - 'custom' engine — reject unless you can verify the code exists and was tested

## Other tools

  - reject_candidate(id, reason) — mark a candidate rejected with a reason
  - insert_active_strategy(symbol, strategy_family, params_json) — for
    brand-new strategies with verified parameters
  - modify_strategy_params(id, new_params) — atomic retire+promote with new params
  - retire_strategy(id, reason) — retire on: cumulative negative P&L
    on real sample, no fires in 14+ days (if active > 14 days), or
    broken regime fit with evidence
  - reactivate_demoted(demotion_id) — restore a previously retired row

You also have the task queue (get_open_tasks/claim_task/complete_task)
and team memory (read_findings/record_finding). Act on promotable
candidates proactively — don't wait for tasks.

Only record a finding when you have learned something materially new
the team would benefit from on a later cycle.
"""

INCLUDE_TOOLS = {
    "insert_active_strategy",
    "reject_candidate",
    "retire_strategy", "promote_candidate", "modify_strategy_params",
    "reactivate_demoted", "get_active_strategies", "get_candidates_queue",
    "get_strategy_detail",
    "claim_task", "complete_task", "get_open_tasks",
    # shared findings memory
    "record_finding", "read_findings", "retract_finding",
    "get_strategy_dossier",
}


def run_registrar_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> AgentDecisionResult:
    """Run one Registrar cycle."""
    started = time.time()
    if os.environ.get("JUDAS_REGISTRAR_AGENT_INHIBIT") == "1":
        log.info("registrar_agent.inhibited_by_env")
        return AgentDecisionResult(
            success=True, actions_taken=[],
            narrative="Registrar inhibited via JUDAS_REGISTRAR_AGENT_INHIBIT.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, schemas = agent_tools.make_tools(
        db_path=db_path, include=INCLUDE_TOOLS, team="registrar",
        claimed_by="registrar_agent", author="registrar",
    )
    return run_agent_loop(
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff="Apply queued registry mutations.",
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )
