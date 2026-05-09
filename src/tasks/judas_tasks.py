"""CrewAI Task definitions for the Judas Swing trading crew.

Four tasks in sequential order:
  Task 1: Market Analysis (MarketAnalyst)
  Task 2: Setup Evaluation (SetupEvaluator) — context from Task 1
  Task 3: Risk Decision (RiskGuardian) — context from Task 2
  Task 4: Trade Execution (TradeExecutor) — context from Task 3

Tasks 2-4 each receive the prior task as context so agents see the full chain.
All tasks request JSON-structured responses for reliable parsing.
"""
from __future__ import annotations

from crewai import Task

from src.agents.judas_agents import (
    make_market_analyst,
    make_setup_evaluator,
    make_risk_guardian,
    make_trade_executor,
)


def make_tasks(symbol: str = "MGC") -> tuple[list[Task], dict]:
    """Create all four tasks and return (tasks_list, agents_dict).

    Args:
        symbol: The futures symbol to analyze (e.g. "MGC").

    Returns:
        Tuple of (ordered task list, dict of agents keyed by name).
    """
    market_analyst = make_market_analyst()
    setup_evaluator = make_setup_evaluator()
    risk_guardian = make_risk_guardian()
    trade_executor = make_trade_executor()

    # ------------------------------------------------------------------
    # Task 1: Market Analysis
    # ------------------------------------------------------------------
    task_market = Task(
        description=(
            f"Analyze the current market conditions for {symbol} on 1H bars.\n\n"
            f"Steps:\n"
            f"1. Call ibkr_data_tool with input '{{\"symbol\": \"{symbol}\"}}' to fetch "
            f"   100 x 1H OHLCV bars plus the prior session high/low.\n"
            f"2. Call judas_detector_tool with the full JSON from step 1 "
            f"   (pass bars, prior_high, prior_low, and symbol='{symbol}').\n"
            f"3. Return a structured market summary.\n\n"
            f"If IBKR is unavailable or returns an error, report the error clearly "
            f"and set pattern_found=false."
        ),
        expected_output=(
            "A JSON object containing:\n"
            "{\n"
            '  "symbol": "MGC",\n'
            '  "bar_count": int,\n'
            '  "prior_high": float,\n'
            '  "prior_low": float,\n'
            '  "latest_bar": {"ts": str, "open": float, "high": float, "low": float, "close": float},\n'
            '  "detector_output": {\n'
            '    "pattern_found": bool,\n'
            '    "direction": "long" | "short" | null,\n'
            '    "sweep": {...} | null,\n'
            '    "choch": {...} | null,\n'
            '    "displacement": {"strength": float, "atr_ratio": float} | null,\n'
            '    "fvg": {"present": bool, "gap_high": float|null, "gap_low": float|null},\n'
            '    "atr_context": {"current_atr": float, "avg_atr_20": float, "contracted": bool},\n'
            '    "stop_price": float | null,\n'
            '    "target_price": float | null,\n'
            '    "risk_dollars": float | null\n'
            "  },\n"
            '  "data_error": null | "error message"\n'
            "}"
        ),
        agent=market_analyst,
    )

    # ------------------------------------------------------------------
    # Task 2: Setup Evaluation
    # ------------------------------------------------------------------
    task_evaluate = Task(
        description=(
            f"Evaluate the {symbol} Judas Swing setup quality from the market analysis.\n\n"
            f"Read the detector_output from Task 1 carefully:\n"
            f"- If pattern_found=false, score is 0 and decision is immediate SKIP.\n"
            f"- If pattern_found=true, score 0-10 using the ICT criteria:\n"
            f"  * Wick sweep (not body): up to 3 points\n"
            f"  * Displacement strength ≥ 1.5×: up to 3 points\n"
            f"  * Clear CHoCH with visible swing pivot: up to 2 points\n"
            f"  * FVG present in displacement: 1 point\n"
            f"  * ATR not contracted: 1 point\n"
            f"- Penalize: body sweep → -2 pts; displacement < 1.5× → immediate skip (score ≤ 4)\n"
            f"- Score ≥ 7 = high quality; 5-6 = marginal; ≤ 4 = skip\n"
            f"- If atr_context.contracted=true, cap score at 4 (RiskGuardian will reject)."
        ),
        expected_output=(
            "A JSON object containing:\n"
            "{\n"
            '  "score": int (0-10),\n'
            '  "recommendation": "TRADE" | "SKIP",\n'
            '  "scoring_breakdown": {\n'
            '    "sweep_type_pts": int,\n'
            '    "displacement_pts": int,\n'
            '    "choch_pts": int,\n'
            '    "fvg_pts": int,\n'
            '    "atr_pts": int\n'
            "  },\n"
            '  "rationale": "Detailed explanation of score and key factors",\n'
            '  "trade_params": {\n'
            '    "entry": float,\n'
            '    "stop": float,\n'
            '    "target": float,\n'
            '    "direction": "long" | "short",\n'
            '    "risk_dollars": float\n'
            '  } | null\n'
            "}"
        ),
        agent=setup_evaluator,
        context=[task_market],
    )

    # ------------------------------------------------------------------
    # Task 3: Risk Decision
    # ------------------------------------------------------------------
    task_risk = Task(
        description=(
            f"Make the final TRADE or SKIP decision for {symbol} based on all risk rules.\n\n"
            f"Steps:\n"
            f"1. Call db_daily_pnl_tool to get today's P&L and trade count.\n"
            f"2. Call db_open_positions_tool to get open position count.\n"
            f"3. Apply risk rules:\n"
            f"   - If daily_pnl <= -300: SKIP (daily loss limit reached)\n"
            f"   - If open_count >= 2: SKIP (max positions reached)\n"
            f"   - If SetupEvaluator score < 6: SKIP (quality gate failed)\n"
            f"   - If atr_context.contracted=true: SKIP (ATR too compressed)\n"
            f"   - If any condition is marginal or unclear: SKIP\n"
            f"4. Output your decision with full reasoning.\n\n"
            f"Remember: patience rule — when in doubt, SKIP. Missing a setup costs "
            f"nothing; taking a bad setup costs real money."
        ),
        expected_output=(
            "A JSON object containing:\n"
            "{\n"
            '  "decision": "TRADE" | "SKIP",\n'
            '  "reasoning": "Detailed explanation of decision and each risk check",\n'
            '  "risk_checks": {\n'
            '    "daily_pnl": float,\n'
            '    "daily_loss_limit_ok": bool,\n'
            '    "open_count": int,\n'
            '    "positions_ok": bool,\n'
            '    "quality_score": int,\n'
            '    "quality_ok": bool,\n'
            '    "atr_ok": bool\n'
            "  },\n"
            '  "trade_params": {\n'
            '    "symbol": "MGC",\n'
            '    "direction": "long" | "short",\n'
            '    "qty": 1,\n'
            '    "entry": float,\n'
            '    "stop": float,\n'
            '    "target": float\n'
            '  } | null\n'
            "}"
        ),
        agent=risk_guardian,
        context=[task_evaluate],
    )

    # ------------------------------------------------------------------
    # Task 4: Trade Execution and Recording
    # ------------------------------------------------------------------
    task_execute = Task(
        description=(
            f"Execute the trade (if approved) and record everything to the database.\n\n"
            f"Steps:\n"
            f"1. Read the RiskGuardian decision from Task 3.\n"
            f"2. IF decision == 'TRADE':\n"
            f"   a. Call ibkr_executor_tool with trade_params from Task 3.\n"
            f"   b. Call db_save_signal_tool with full signal data (risk_decision='TRADE').\n"
            f"   c. Call db_save_trade_tool with signal_id from step (b) + order details.\n"
            f"3. IF decision == 'SKIP':\n"
            f"   a. Call db_save_signal_tool with signal data (risk_decision='SKIP').\n"
            f"   b. Do NOT call ibkr_executor_tool or db_save_trade_tool.\n"
            f"4. Return a final run summary.\n\n"
            f"Always record signals to DB even on SKIP — complete records are essential "
            f"for performance tracking and Phase 2 IterationAgent."
        ),
        expected_output=(
            "A JSON object containing:\n"
            "{\n"
            '  "run_summary": {\n'
            '    "symbol": "MGC",\n'
            '    "decision": "TRADE" | "SKIP",\n'
            '    "signal_id": int | null,\n'
            '    "trade_id": int | null,\n'
            '    "ibkr_order_id": int | null,\n'
            '    "order_status": "placed" | "error" | "skipped",\n'
            '    "message": "Human-readable summary of what happened"\n'
            "  }\n"
            "}"
        ),
        agent=trade_executor,
        context=[task_risk],
    )

    tasks = [task_market, task_evaluate, task_risk, task_execute]
    agents = {
        "market_analyst": market_analyst,
        "setup_evaluator": setup_evaluator,
        "risk_guardian": risk_guardian,
        "trade_executor": trade_executor,
    }
    return tasks, agents
