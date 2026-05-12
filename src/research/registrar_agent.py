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
keep the strategy registry healthy and productive so the trading
runtime has good coverage. The goal of the whole lab is absolute
dollar P&L, which means more strategies firing, not fewer.

Tools you can use directly:
  - insert_active_strategy(symbol, strategy_family, params_json) —
    create a brand-new active row from scratch.
  - promote_candidate(candidate_id) — promote from the researcher's
    candidate queue. Multiple actives per (symbol, family) are
    allowed and encouraged for diversification.
  - modify_strategy_params(id, new_params) — atomic retire+promote
    of an existing active with new params.
  - retire_strategy(id, reason) — retire only on real evidence
    (cumulative negative P&L on a real sample, broken regime fit,
    no fires in 14+ days). Don't retire to consolidate.
  - reactivate_demoted(demotion_id) — bring back a previously
    retired row.

You also have the task queue (get_open_tasks/claim_task/complete_task)
and the team memory (read_findings/record_finding) — but you are not
limited to claimed tasks. If there's promotable work in the candidate
queue or an obvious coverage gap (a symbol with zero active
strategies), act on it directly.

Only record a finding when you have learned something materially new
the team would benefit from on a later cycle. Do not write a finding
just because a cycle ended.
"""

INCLUDE_TOOLS = {
    "insert_active_strategy",
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
