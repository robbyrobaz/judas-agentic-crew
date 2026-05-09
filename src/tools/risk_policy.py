"""Deterministic hard-risk filters for Judas trade decisions."""
from __future__ import annotations

import json

from crewai.tools import tool

from src.config import load_config


def evaluate_risk_policy(
    *,
    detector_output: dict,
    quality_score: int,
    daily_pnl: float,
    open_count: int,
    trades_today: int,
    can_trade_now: bool,
    strategy_params: dict | None = None,
) -> dict:
    """Apply deterministic hard filters and return a structured decision."""
    runtime = load_config()
    cfg = runtime.risk
    params = strategy_params or {}
    daily_loss_limit = float(params.get("daily_loss_limit_dollars", cfg.daily_loss_limit_dollars))
    max_open_positions = int(params.get("max_open_positions", cfg.max_open_positions))
    max_trades_per_day = int(params.get("max_trades_per_day", cfg.max_trades_per_day))
    quality_score_min = int(params.get("quality_score_min", runtime.crew.min_quality_score))
    min_atr_ratio = float(params.get("min_atr_ratio", cfg.atr_contraction_min_ratio))
    min_displacement_strength = float(params.get("min_displacement_strength", cfg.min_displacement_strength))
    min_body_ratio = float(params.get("min_displacement_body_ratio", cfg.min_displacement_body_ratio))
    max_sweep_age_bars = int(params.get("max_sweep_age_bars", cfg.max_sweep_age_bars))
    require_fvg = bool(params.get("require_fvg", False))

    reasons: list[str] = []
    checks: dict[str, object] = {
        "session_ok": bool(can_trade_now),
        "pattern_ok": bool(detector_output.get("pattern_found")),
        "daily_loss_limit_ok": daily_pnl > daily_loss_limit,
        "positions_ok": open_count < max_open_positions,
        "trades_per_day_ok": trades_today < max_trades_per_day,
        "quality_ok": quality_score >= quality_score_min,
        "atr_ok": True,
        "displacement_ok": True,
        "body_ok": True,
        "sweep_fresh_ok": True,
        "fvg_ok": True,
    }

    if not checks["session_ok"]:
        reasons.append("session gate failed")
    if not checks["pattern_ok"]:
        reasons.append("no valid Judas pattern found")
    if not checks["daily_loss_limit_ok"]:
        reasons.append(f"daily loss limit hit ({daily_pnl:.2f} <= {daily_loss_limit:.2f})")
    if not checks["positions_ok"]:
        reasons.append(f"max open positions reached ({open_count} >= {max_open_positions})")
    if not checks["trades_per_day_ok"]:
        reasons.append(f"max trades per day reached ({trades_today} >= {max_trades_per_day})")
    if not checks["quality_ok"]:
        reasons.append(f"quality score below threshold ({quality_score} < {quality_score_min})")

    if detector_output.get("pattern_found"):
        atr_context = detector_output.get("atr_context") or {}
        current_atr = float(atr_context.get("current_atr") or 0.0)
        avg_atr_20 = float(atr_context.get("avg_atr_20") or 0.0)
        atr_ok = avg_atr_20 > 0 and current_atr >= min_atr_ratio * avg_atr_20
        checks["atr_ok"] = atr_ok
        if not atr_ok:
            reasons.append(
                f"ATR too compressed ({current_atr:.4f} < "
                f"{min_atr_ratio:.2f} x {avg_atr_20:.4f})"
            )

        displacement = detector_output.get("displacement") or {}
        strength = float(displacement.get("strength") or 0.0)
        body_ratio = float(displacement.get("body_ratio") or 0.0)
        displacement_ok = strength >= min_displacement_strength
        body_ok = body_ratio >= min_body_ratio
        checks["displacement_ok"] = displacement_ok
        checks["body_ok"] = body_ok
        if not displacement_ok:
            reasons.append(
                f"displacement too weak ({strength:.3f} < {min_displacement_strength:.2f})"
            )
        if not body_ok:
            reasons.append(
                f"displacement body too small ({body_ratio:.3f} < {min_body_ratio:.2f})"
            )

        sweep = detector_output.get("sweep") or {}
        choch = detector_output.get("choch") or {}
        if sweep and choch:
            sweep_age_bars = int(choch.get("bar_idx", 0)) - int(sweep.get("bar_idx", 0))
        else:
            sweep_age_bars = 999
        sweep_fresh_ok = sweep_age_bars <= max_sweep_age_bars
        checks["sweep_fresh_ok"] = sweep_fresh_ok
        checks["sweep_age_bars"] = sweep_age_bars
        if not sweep_fresh_ok:
            reasons.append(
                f"sweep too old ({sweep_age_bars} bars > {max_sweep_age_bars})"
            )

        if require_fvg:
            fvg_present = bool((detector_output.get("fvg") or {}).get("present"))
            checks["fvg_ok"] = fvg_present
            if not fvg_present:
                reasons.append("FVG required by active strategy but not present")

    decision = "TRADE" if not reasons else "SKIP"
    return {
        "decision": decision,
        "reasons": reasons if reasons else ["all hard filters passed"],
        "checks": checks,
        "strategy_params": params,
    }


@tool("risk_policy_tool")
def risk_policy_tool(input_json: str) -> str:
    """Apply deterministic hard filters to setup + session + DB state."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    result = evaluate_risk_policy(
        detector_output=data.get("detector_output") or {},
        quality_score=int(data.get("quality_score", 0)),
        daily_pnl=float(data.get("daily_pnl", 0.0)),
        open_count=int(data.get("open_count", 0)),
        trades_today=int(data.get("trades_today", 0)),
        can_trade_now=bool(data.get("can_trade_now", False)),
        strategy_params=data.get("strategy_params") or {},
    )
    return json.dumps(result)
