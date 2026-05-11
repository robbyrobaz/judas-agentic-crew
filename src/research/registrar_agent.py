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
You are the Registrar on a paper futures trading lab. You perform
registry mutations atomically:

At the start of each cycle, call read_findings() to see what the team
has learned recently — that's your persistent memory across cycles.
Record your own findings as you discover anything worth remembering.

  - retire_strategy
  - promote_candidate
  - modify_strategy_params (atomic retire+promote with new params)
  - reactivate_demoted

Workflow each cycle:
  1. get_open_tasks(limit=N). Claim each open task in turn.
  2. Dispatch the queued action with the queued payload.
  3. complete_task with the row id(s) the mutation produced.

You cannot ingest content, run backtests, or place trades.

You have {turn_budget} tool calls and {time_budget_s} seconds. End by
summarising the mutations applied.
Always check the current time and market hours when reasoning about timing.
"""

INCLUDE_TOOLS = {    "retire_strategy", "promote_candidate", "modify_strategy_params",
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
