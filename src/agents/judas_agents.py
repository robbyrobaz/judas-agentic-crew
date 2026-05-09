"""CrewAI Agent definitions for the Judas Swing trading crew.

Four agents:
  MarketAnalyst   — fetches bars, runs detector
  SetupEvaluator  — scores the setup quality 0-10
  RiskGuardian    — checks DB + risk limits, outputs TRADE|SKIP
  TradeExecutor   — places paper order, records to DB

LLM: MiniMax M2.1 via minimax/ prefix (LiteLLM native MiniMax support).
Knowledge base: judas_concepts.md + research_findings.md loaded from knowledge_base/.
"""
from __future__ import annotations

import os
from pathlib import Path

from crewai import Agent, LLM
from crewai.knowledge.source.string_knowledge_source import StringKnowledgeSource

from src.tools.ibkr_data import ibkr_data_tool
from src.tools.ibkr_executor import ibkr_executor_tool
from src.tools.judas_detector import judas_detector_tool
from src.tools.db_tools import (
    db_daily_pnl_tool,
    db_open_positions_tool,
    db_save_signal_tool,
    db_save_trade_tool,
)

_KB_DIR = Path(__file__).parent.parent.parent / "knowledge_base"


def _load_knowledge_text() -> str:
    """Load both KB files into a single string for StringKnowledgeSource."""
    parts = []
    for fname in ("judas_concepts.md", "research_findings.md"):
        fpath = _KB_DIR / fname
        if fpath.exists():
            parts.append(f"# FILE: {fname}\n\n{fpath.read_text()}")
    return "\n\n---\n\n".join(parts)


def _make_llm() -> LLM:
    """Construct MiniMax M2.1 LLM instance."""
    return LLM(
        model="minimax/MiniMax-M2.1",
        api_key=os.environ.get("MINIMAX_API_KEY", ""),
        api_base="https://api.minimax.io/v1",
        temperature=0.0,
        max_tokens=4096,
    )


def _make_knowledge_source() -> list:
    """Return a list containing a StringKnowledgeSource with KB content."""
    kb_text = _load_knowledge_text()
    if not kb_text:
        return []
    return [StringKnowledgeSource(content=kb_text)]


def make_market_analyst() -> Agent:
    """Agent 1: Fetches IBKR data and runs the Judas detector."""
    return Agent(
        role="Futures Market Data Analyst",
        goal=(
            "Fetch the current 1H OHLCV bars for the target symbol from IBKR, "
            "then run the deterministic Judas sweep+CHoCH detector on those bars. "
            "Return a structured market summary including the full detector output."
        ),
        backstory=(
            "You are an expert in reading futures market structure with deep knowledge "
            "of ICT (Inner Circle Trader) concepts. You know that 5-minute Judas Swing "
            "on MGC produced -$93 net with -0.21R expectancy — 5m is noise-killed. "
            "You work exclusively on 1H bars where the sweep+CHoCH logic has real edge. "
            "You run the deterministic sweep+CHoCH detector and report the raw output "
            "faithfully — you do not interpret or filter, only measure and report."
        ),
        tools=[ibkr_data_tool, judas_detector_tool],
        llm=_make_llm(),
        knowledge_sources=_make_knowledge_source(),
        verbose=True,
        allow_delegation=False,
        max_iter=5,
    )


def make_setup_evaluator() -> Agent:
    """Agent 2: Scores the detected setup 0-10."""
    return Agent(
        role="ICT Setup Quality Evaluator",
        goal=(
            "Score the detected Judas Swing setup on a 0-10 quality scale using deep "
            "ICT knowledge. Return a JSON object with: score (int), rationale (str), "
            "and trade_params (dict or null if not trading)."
        ),
        backstory=(
            "You are a master of ICT (Inner Circle Trader) concepts with years of "
            "experience distinguishing high-quality setups from noise. "
            "You know the 'best setups only' criteria by heart: "
            "(1) Clean WICK sweep — close must be back inside the swept level. "
            "    Body sweeps are lower quality (cap score at 6). "
            "(2) Displacement strength ≥ 1.5× — the CHoCH bar must be impulsive. "
            "    Below 1.5× means no real reversal energy (cap score at 5). "
            "(3) Clear structural CHoCH — a swing pivot must exist and be broken. "
            "(4) FVG present — adds confidence in the displacement (bonus point). "
            "(5) ATR not contracted — no edge in compressed markets. "
            "Score 7-10 = high quality (recommend trade). "
            "Score 5-6 = marginal (RiskGuardian will likely reject). "
            "Score 0-4 = clear skip. "
            "If no pattern was detected, score is 0. "
            "You reason from the MarketAnalyst's output — you do not call any tools."
        ),
        tools=[],
        llm=_make_llm(),
        knowledge_sources=_make_knowledge_source(),
        verbose=True,
        allow_delegation=False,
        max_iter=5,
    )


def make_risk_guardian() -> Agent:
    """Agent 3: Checks risk limits and outputs TRADE or SKIP."""
    return Agent(
        role="Strict Risk Gatekeeper",
        goal=(
            "Check all risk conditions and output either TRADE or SKIP with full reasoning. "
            "Never approve a marginal setup. When in doubt: SKIP."
        ),
        backstory=(
            "You are an extremely disciplined risk manager. Your only job is to protect "
            "capital by enforcing strict risk rules. You check: "
            "(1) Daily P&L: if today's realized P&L is below -$300, SKIP all trades. "
            "(2) Open positions: if 2 or more positions are already open, SKIP. "
            "(3) Setup quality: if SetupEvaluator score < 6, SKIP immediately. "
            "(4) ATR contraction: if atr_context.contracted=true, SKIP — no edge in "
            "    compressed ranges. "
            "(5) Patience rule: if ANY condition is marginal or unclear, SKIP. "
            "    You would rather miss 10 real setups than take 1 bad one. "
            "You do NOT apply a NY-open lockout. NY 09:30–10:30 ET is the PRIME Judas "
            "window — the sweep often occurs in the first 30 minutes. A lockout here "
            "would skip the best setups of the day. "
            "Output a JSON dict: {decision: 'TRADE' or 'SKIP', reasoning: str}. "
            "If TRADE: also include entry, stop, target, qty from prior context."
        ),
        tools=[db_daily_pnl_tool, db_open_positions_tool],
        llm=_make_llm(),
        knowledge_sources=_make_knowledge_source(),
        verbose=True,
        allow_delegation=False,
        max_iter=5,
    )


def make_trade_executor() -> Agent:
    """Agent 4: Places paper order and records everything to DB."""
    return Agent(
        role="IBKR Paper Order Executor and Trade Recorder",
        goal=(
            "If RiskGuardian decision is TRADE: place the paper order on IBKR and "
            "record both the signal and trade to the database. "
            "If decision is SKIP: record the signal (with SKIP status) to the database. "
            "Always confirm what was done at the end."
        ),
        backstory=(
            "You are a precise and disciplined executor. You place orders exactly as "
            "specified — no deviations from the entry, stop, and target provided by "
            "prior agents. You never second-guess the risk decision. "
            "You record EVERY signal to the database, even skips, because performance "
            "tracking requires a complete record of all evaluated setups. "
            "After placing an order you confirm the IBKR order ID. "
            "After recording to DB you confirm the signal_id and trade_id. "
            "You operate in PAPER MODE ONLY — the executor tool will reject any "
            "attempt to place live orders."
        ),
        tools=[ibkr_executor_tool, db_save_signal_tool, db_save_trade_tool],
        llm=_make_llm(),
        knowledge_sources=_make_knowledge_source(),
        verbose=True,
        allow_delegation=False,
        max_iter=8,
    )
