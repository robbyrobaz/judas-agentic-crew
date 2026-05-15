"""Operator agent (Phase 10) — manager that delegates.

Tools = delegations + reads. NOT actions. The four ``delegate_to_*``
tools enqueue rows on ``agent_tasks`` for the specialists to consume.
The Operator does not retire/promote/place_bracket/ingest directly —
those belong to the Researcher, Trader, Registrar, and Coder.

Tests monkeypatch ``src.research.pm_agent._call_llm`` (shared seam).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
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


# Shared between the Operator agent and the dashboard chat.
# Single source of truth so the chat's framing can't drift from the
# Operator's mandate.
GOALS_PREAMBLE = """\
The goal of this lab is to make as much ABSOLUTE DOLLAR P&L as possible
compounded over a long horizon on the $5,000 IBKR paper sleeve.

Profit factor (PF) is a QUALITY GATE, not the optimization target.
A strategy needs PF reasonably above 1.0 (positive expectancy with
statistical confidence) to be considered. But once it clears that gate,
the question is dollar throughput: P&L per fire × fires per period.
A PF-11 strategy firing 4 times a year earns less than a PF-2 strategy
firing 200 times a year at the same per-trade risk.

Concretely:
  - Need PF > 1 with a real sample (>= 15 closed trades preferred).
  - Optimize for absolute dollar earnings, not for chasing the highest PF.
  - Don't reflexively consolidate to "best PF per symbol per family."
    Multiple strategies per symbol are fine if each one is producing
    independent positive P&L on a real sample. Diversity reduces
    blow-up risk and increases dollar capture across regimes.
  - Real retire signals: cumulative negative P&L on >20 trades, max
    consecutive losers >= 6, no fires in 14+ days, or a clearly broken
    regime fit. NOT "this variant has lower PF than that variant."
  - When promoting: prefer dollar-earners with real samples over
    high-PF / tiny-sample variants. Walk-forward robustness matters,
    but so does "will this fire enough times to actually make money?"
"""


SYSTEM_PROMPT = """\
You are the manager of a paper futures trading lab. Your job is to
make as much money as possible on the IBKR paper account.

""" + GOALS_PREAMBLE + """

You have a team — Researcher, Trader, Registrar, Coder — and the
delegation tools to coordinate them. You also have the read tools
(get_active_strategies, get_open_positions, get_recent_pnl,
get_candidates_queue, get_workshop_leaderboard, query_db,
get_outstanding_delegations, get_recent_trades) and the team memory
(read_findings, record_finding).

There is no fixed cycle order. Look at the state, decide what's most
valuable to do this cycle, and delegate it. Examples of high-value
work:
  - Live positions need protection / closing → delegate_to_trader.
  - Strategy coverage is thin or stale → delegate_to_registrar to
    promote candidates or insert new active strategies.
  - You see a recurring failure pattern → delegate_to_coder.
  - There's a researcher backlog or an emerging regime worth
    digging into → delegate_to_researcher.

Only record a finding when you've learned something materially new
that future cycles will use. Don't write a finding just because a
cycle ended.
"""


# Operator's palette: delegations + reads only. Explicitly NOT action tools.
INCLUDE_TOOLS = {    # delegations
    "delegate_to_researcher", "delegate_to_trader",
    "delegate_to_registrar", "delegate_to_coder",
    # reads
    "get_active_strategies", "get_recent_pnl", "get_recent_briefs",
    "get_outstanding_delegations", "get_recent_trades",
    "get_candidates_queue", "get_workshop_leaderboard", "query_db",
    "get_strategy_detail", "get_recent_experiments", "get_open_positions",
    "get_regime_tag",
    # shared findings memory
    "record_finding", "read_findings", "retract_finding",
    "get_strategy_dossier",
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


def _build_operator_kickoff(db_path: str) -> str:
    """Pre-load compact state into the operator's first message.

    Same pattern as researcher_agent._build_kickoff: gives the LLM
    everything it needs to delegate immediately on turn 1, without
    calling discovery tools that would balloon the context and overflow.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"=== OPERATOR BRIEFING — {now} ===\n"]

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Active strategies
        rows = conn.execute("""
            SELECT symbol, strategy_family, version, params_json
            FROM active_strategies WHERE state='active'
            ORDER BY symbol, strategy_family
        """).fetchall()
        all_syms = {"MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"}
        active_syms: set[str] = set()
        lines.append(f"ACTIVE STRATEGIES ({len(rows)}):")
        for r in rows:
            p = json.loads(r["params_json"] or "{}")
            name = p.get("strategy_name") or p.get("strategy_type") or "?"
            lines.append(f"  {r['symbol']} {r['strategy_family']} v{r['version']} — {name}")
            active_syms.add(r["symbol"])
        uncovered = sorted(all_syms - active_syms)
        if uncovered:
            lines.append(f"  *** UNCOVERED: {', '.join(uncovered)} — delegate research ***")
        lines.append("")

        # Candidates awaiting review (compact — just key metrics)
        cand_rows = conn.execute("""
            SELECT id, ts_utc, symbol, strategy_family, decision, metrics_json,
                   substr(rationale, 1, 100) AS rationale_preview
            FROM strategy_candidates
            WHERE status='candidate'
            ORDER BY ts_utc DESC LIMIT 20
        """).fetchall()
        total_cands = conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates WHERE status='candidate'"
        ).fetchone()[0]
        if cand_rows:
            lines.append(f"CANDIDATES AWAITING REVIEW ({total_cands} total, showing top 20):")
            for c in cand_rows:
                m = json.loads(c["metrics_json"] or "{}")
                pf = m.get("profit_factor") or m.get("pf_20") or m.get("pf") or "?"
                trades = m.get("total_trades") or m.get("n_trades") or "?"
                lines.append(
                    f"  #{c['id']} {c['symbol']} {c['strategy_family']} "
                    f"PF={pf} trades={trades} | {c['rationale_preview']}"
                )
            lines.append("  → Use delegate_to_registrar to promote or reject candidates.")
            lines.append("")

        # Recent P&L
        trade_rows = conn.execute("""
            SELECT symbol, direction, pnl_dollars, status, opened_at
            FROM trades
            WHERE datetime(COALESCE(closed_at, opened_at)) >= datetime('now', '-7 days')
            ORDER BY opened_at DESC LIMIT 10
        """).fetchall()
        if trade_rows:
            total_pnl = sum(float(r["pnl_dollars"] or 0) for r in trade_rows if r["status"] == "closed")
            lines.append(f"RECENT TRADES (last 7 days, total PnL ${total_pnl:+.2f}):")
            for r in trade_rows:
                pnl = f"${float(r['pnl_dollars'] or 0):+.2f}" if r["pnl_dollars"] is not None else "open"
                lines.append(f"  {r['symbol']} {r['direction']} {r['status']} {pnl}")
            lines.append("")

        # Open tasks from all teams
        task_rows = conn.execute("""
            SELECT team, action, urgency, substr(rationale, 1, 80) AS r
            FROM agent_tasks WHERE status='open'
            ORDER BY CASE urgency WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     requested_at_utc ASC
            LIMIT 15
        """).fetchall()
        if task_rows:
            lines.append(f"OPEN TEAM TASKS ({len(task_rows)}):")
            for t in task_rows:
                lines.append(f"  [{t['team']}] {t['action']} [{t['urgency']}]: {t['r']}")
            lines.append("")

        # Last brief
        brief = conn.execute("""
            SELECT brief_date, substr(content_md, 1, 300) AS preview
            FROM daily_briefs ORDER BY brief_date DESC LIMIT 1
        """).fetchone()
        if brief:
            lines.append(f"LAST BRIEF ({brief['brief_date']}):")
            lines.append(str(brief["preview"]))
            lines.append("")

        conn.close()
    except Exception:
        log.exception("operator_agent._build_kickoff failed")

    lines.append("Your job: look at the above, decide what to delegate. Act now.")
    return "\n".join(lines)


def run_operator_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
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
        author="operator",
    )

    try:
        date_et = datetime.now(_ET_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        date_et = datetime.now(timezone.utc).isoformat()

    today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    preload = _build_operator_kickoff(db_path)
    base = run_agent_loop(
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff=(
            f"{preload}\n\n"
            f"It's {date_et}. The briefing above is pre-loaded — do NOT call "
            f"get_active_strategies, get_candidates_queue, or get_recent_pnl "
            f"(that data is already above). Use your turns to delegate.\n\n"
            f"If this is the 21:00 UTC run (post-NY close): review the candidates "
            f"above and delegate promote/reject to registrar; write a daily brief as "
            f"record_finding with title 'DAILY_BRIEF:{today_date}' covering trades, "
            f"regime, research wins/losses, tomorrow's watch list; then delegate "
            f"next-day research priorities to the Researcher task queue.\n\n"
            f"If this is the 06:00 UTC run (pre-London): review candidates, "
            f"delegate promote/reject for any clear winners, check strategies with "
            f"0 fires in 14+ days, update regime tag, write pre-session brief as "
            f"record_finding with title 'SESSION_BRIEF:{today_date}:pre-london'."
        ),
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
