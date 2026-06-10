"""CrewAI Agent definitions for the Judas Swing trading crew.

Four agents:
  MarketAnalyst   — fetches bars, runs detector
  SetupEvaluator  — scores the setup quality 0-10
  RiskGuardian    — checks DB + risk limits, outputs TRADE|SKIP
  TradeExecutor   — places paper order, records to DB

LLM: config-driven MiniMax M3 via the OpenAI-compatible endpoint.
Knowledge base: local research plus selected workshop findings.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from crewai import Agent, LLM

from src.agents.operator_context import build_operator_appendix
from src.tools.ibkr_data import ibkr_data_tool
from src.tools.ibkr_executor import ibkr_executor_tool
from src.tools.judas_detector import judas_detector_tool
from src.tools.db_tools import (
    db_daily_pnl_tool,
    db_open_positions_tool,
    db_save_signal_tool,
    db_save_trade_tool,
)
from src.tools.session_tools import session_status_tool
from src.tools.risk_policy import risk_policy_tool
from src.config import load_config

_KB_DIR = Path(__file__).parent.parent.parent / "knowledge_base"


def _resolve_workshop_root() -> Path | None:
    env_path = os.environ.get("JUDAS_WORKSHOP_PATH")
    if env_path:
        root = Path(env_path).expanduser()
        return root if root.exists() else None

    sibling = _KB_DIR.parent.parent / "judas-futures-workshop"
    return sibling if sibling.exists() else None


def _read_optional_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()


@lru_cache(maxsize=1)
def _load_knowledge_text() -> str:
    """Load local KB plus selected workshop context into one string."""
    parts = []
    for fname in (
        "judas_concepts.md",
        "research_findings.md",
        "market_hours.md",
        "workshop_context.md",
    ):
        fpath = _KB_DIR / fname
        text = _read_optional_text(fpath)
        if text:
            parts.append(f"# FILE: {fname}\n\n{text}")

    workshop_root = _resolve_workshop_root()
    if workshop_root is not None:
        workshop_files = (
            workshop_root / "RESEARCH_FINDINGS.md",
            workshop_root / "STATUS.md",
        )
        for fpath in workshop_files:
            text = _read_optional_text(fpath)
            if text:
                parts.append(f"# WORKSHOP FILE: {fpath.name}\n\n{text}")
    return "\n\n---\n\n".join(parts)


@lru_cache(maxsize=1)
def _load_runtime_config():
    return load_config()


def _make_llm() -> LLM:
    """Construct the configured MiniMax/OpenAI-compatible LLM instance."""
    cfg = _load_runtime_config()
    additional_params: dict[str, object] = {}
    if cfg.crew.llm_reasoning_split:
        additional_params["extra_body"] = {"reasoning_split": True}

    return LLM(
        model=cfg.crew.llm_model,
        api_key=os.environ.get("MINIMAX_API_KEY", ""),
        api_base=cfg.crew.llm_base_url,
        provider=cfg.crew.llm_provider,
        temperature=0.0,
        max_tokens=4096,
        additional_params=additional_params,
    )


def build_llm() -> LLM:
    """Public wrapper for operational checks and one-off calls."""
    return _make_llm()


@lru_cache(maxsize=1)
def _knowledge_appendix() -> str:
    kb_text = _load_knowledge_text().strip()
    if not kb_text:
        return ""
    max_chars = 12000
    if len(kb_text) > max_chars:
        kb_text = kb_text[:max_chars] + "\n\n[knowledge truncated for prompt budget]"
    return (
        f"\n\nReference knowledge you should treat as established context:\n{kb_text}"
        f"{build_operator_appendix()}"
    )


def make_market_analyst() -> Agent:
    """Agent 1: Fetches IBKR data and runs the Judas detector."""
    cfg = _load_runtime_config()
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
        ) + _knowledge_appendix(),
        tools=[ibkr_data_tool, judas_detector_tool],
        llm=_make_llm(),
        verbose=cfg.crew.verbose,
        allow_delegation=False,
        max_iter=5,
    )


def make_setup_evaluator() -> Agent:
    """Agent 2: Scores the detected setup 0-10."""
    cfg = _load_runtime_config()
    return Agent(
        role="ICT Setup Quality Evaluator",
        goal=(
            "Score the detected Judas Swing setup on a 0-10 quality scale using deep "
            "ICT knowledge. Return a JSON object with: score (int), rationale (str), "
            "and trade_params (dict or null if not trading)."
        ),
        backstory=(
            "You are a master of ICT (Inner Circle Trader) concepts with years of "
            "experience reading setups. Quality signals you weigh (guidelines, not "
            "hard caps — use judgment): "
            "(1) Clean WICK sweep — close back inside the swept level is strongest; "
            "    body sweeps are weaker but can still work. "
            "(2) Displacement strength — impulsive CHoCH bars (≥1.5×) are best; "
            "    softer displacement is lower-conviction but not disqualifying. "
            "(3) Clear structural CHoCH — a swing pivot broken. "
            "(4) FVG present — adds confidence. "
            "(5) ATR context — compressed ranges are lower-edge. "
            "Score generously when there's a plausible setup: this is a paper lab and "
            "marginal setups still produce useful live data. "
            "If no pattern was detected, score is 0. "
            "If no pattern was detected, score is 0. "
            "You reason from the MarketAnalyst's output — you do not call any tools."
        ) + _knowledge_appendix(),
        tools=[],
        llm=_make_llm(),
        verbose=cfg.crew.verbose,
        allow_delegation=False,
        max_iter=5,
    )


def make_risk_guardian() -> Agent:
    """Agent 3: Checks risk limits and outputs TRADE or SKIP."""
    cfg = _load_runtime_config()
    return Agent(
        role="Strict Risk Gatekeeper",
        goal=(
            "Check risk conditions and output TRADE or SKIP with full reasoning. "
            "Bias toward ACTION — this is a paper account whose purpose is to gather "
            "live evidence, so take setups that have a plausible edge. Reserve SKIP "
            "for genuine risk-limit breaches, not mere marginality."
        ),
        backstory=(
            "You are a pragmatic risk manager who knows that on a paper lab the goal is "
            "to LEARN from live trades, not to sit on the sidelines. You allow setups "
            "with edge through and only block on real limit breaches. You check: "
            "(1) Daily P&L: if today's realized P&L is below -$300, SKIP all trades. "
            "(2) Open positions: if 2 or more positions are already open, SKIP. "
            "(3) Setup quality: if SetupEvaluator score is below the active strategy minimum, SKIP immediately. "
            "(4) ATR contraction: if atr_context.contracted=true, SKIP — no edge in "
            "    compressed ranges. "
            "(5) Session timing: only open new trades during the configured live "
            "    entry windows, and never hold past the hard flat deadline. "
            "(6) Active strategy policy: use the active paper strategy parameters "
            "    from MarketAnalyst context and apply them through the deterministic risk tool. "
            "(6) Patience rule: if ANY condition is marginal or unclear, SKIP. "
            "    You would rather miss 10 real setups than take 1 bad one. "
            "NY open remains the prime Judas window, but this repo is prop-firm safe: "
            "it uses constrained entry windows and a mandatory end-of-day flat policy. "
            "Output a JSON dict: {decision: 'TRADE' or 'SKIP', reasoning: str}. "
            "If TRADE: also include entry, stop, target, qty from prior context."
        ) + _knowledge_appendix(),
        tools=[db_daily_pnl_tool, db_open_positions_tool, session_status_tool, risk_policy_tool],
        llm=_make_llm(),
        verbose=cfg.crew.verbose,
        allow_delegation=False,
        max_iter=5,
    )


def make_trade_executor() -> Agent:
    """Agent 4: Places paper order and records everything to DB."""
    cfg = _load_runtime_config()
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
        ) + _knowledge_appendix(),
        tools=[ibkr_executor_tool, db_save_signal_tool, db_save_trade_tool],
        llm=_make_llm(),
        verbose=cfg.crew.verbose,
        allow_delegation=False,
        max_iter=8,
    )
