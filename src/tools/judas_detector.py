"""Deterministic Judas Swing detector — 1H timeframe edition.

Ported from judas-futures-workshop/src/strategies/judas.py but:
  - Standalone (no imports from workshop codebase)
  - Tuned for 1H bars: confirmation_bars=4, min_sweep_ticks=3
  - Returns plain dict instead of Signal dataclass (CrewAI tool-friendly)

Background from research (baked in so agents know):
  5-minute Judas produced -$93 net / 26% WR / -0.21R expectancy on MGC.
  Root cause: 5m is too noisy for reliable sweep+CHoCH timing on liquid futures.
  1H timeframe shows real edge: 40/81 combos profitable on NQ 1H.
  Best combo: RSI 25/75 mean-revert at 1H (+0.30R expectancy, ~1 trade/week).

This module exposes two things:
  1. `run_judas_detection()` — pure Python function for programmatic use.
  2. `judas_detector_tool` — CrewAI @tool wrapper (see bottom of file).
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def find_swing_pivots(bars: pd.DataFrame, length: int = 2) -> list[tuple[int, str, float]]:
    """Return list of (idx, kind, price) for swing highs/lows.

    A pivot high at index i requires `length` bars on either side with strictly
    lower highs. A pivot low requires strictly higher lows on both sides.

    Args:
        bars: DataFrame with 'high' and 'low' columns.
        length: Number of confirming bars required on each side.

    Returns:
        List of (bar_index, "high"|"low", price).
    """
    pivots: list[tuple[int, str, float]] = []
    n = len(bars)
    if n < 2 * length + 1:
        return pivots
    highs = bars["high"].values
    lows = bars["low"].values
    for i in range(length, n - length):
        is_pivot_high = all(
            highs[i] > highs[i - k] and highs[i] > highs[i + k]
            for k in range(1, length + 1)
        )
        if is_pivot_high:
            pivots.append((i, "high", float(highs[i])))
            continue
        is_pivot_low = all(
            lows[i] < lows[i - k] and lows[i] < lows[i + k]
            for k in range(1, length + 1)
        )
        if is_pivot_low:
            pivots.append((i, "low", float(lows[i])))
    return pivots


# ---------------------------------------------------------------------------
# Sweep detection
# ---------------------------------------------------------------------------

def detect_sweep(
    bars: pd.DataFrame,
    prior_high: float,
    prior_low: float,
    from_idx: int = 0,
    most_recent: bool = True,
    min_ticks: float = 0.0,
) -> Optional[tuple[str, int, float]]:
    """Find a bar that sweeps either the prior session high or low.

    most_recent=True (default): return the LATEST sweep in the window.
    This ensures the CHoCH check evaluates against the freshest setup.

    min_ticks: minimum distance (in price units) beyond the level for the
    sweep to count. Pass (min_sweep_ticks * tick_size) from the symbol config.

    Returns:
        ("high"|"low", bar_index, extreme_price) or None.
    """
    if bars.empty:
        return None
    sub = bars.iloc[from_idx:]
    rows = list(sub.itertuples(index=False))
    if most_recent:
        rows = list(reversed(rows))
        indices = list(range(from_idx + len(sub) - 1, from_idx - 1, -1))
    else:
        indices = list(range(from_idx, from_idx + len(sub)))

    for i, row in zip(indices, rows):
        if row.high > prior_high + min_ticks:
            return ("high", i, float(row.high))
        if row.low < prior_low - min_ticks:
            return ("low", i, float(row.low))
    return None


# ---------------------------------------------------------------------------
# CHoCH detection
# ---------------------------------------------------------------------------

def detect_choch(
    bars: pd.DataFrame,
    sweep_idx: int,
    sweep_kind: str,
    lookforward: int = 4,
    pivot_len: int = 2,
) -> Optional[tuple[str, int, float]]:
    """After a sweep, look forward up to `lookforward` bars for a CHoCH.

    Sweep of HIGH (expect short) → need a close BELOW the most recent swing
    low that formed before the sweep. Sweep of LOW (expect long) → need a
    close ABOVE the most recent swing high.

    On 1H bars, lookforward=4 is tighter than the 5m workshop default of 6,
    which eliminates many of the noise-driven false confirmations.

    Returns:
        ("long"|"short", choch_bar_idx, broken_pivot_price) or None.
    """
    n = len(bars)
    # Only consider pivots visible at sweep time
    visible_end = min(sweep_idx + pivot_len + 1, n)
    pivots = find_swing_pivots(bars.iloc[:visible_end], length=pivot_len)

    if sweep_kind == "high":
        opposing = [p for p in pivots if p[1] == "low" and p[0] < sweep_idx]
        if not opposing:
            log.debug("choch.no_opposing_low", extra={"sweep_idx": sweep_idx})
            return None
        broken_level = opposing[-1][2]
        for j in range(sweep_idx + 1, min(sweep_idx + 1 + lookforward, n)):
            if bars.iloc[j]["close"] < broken_level:
                return ("short", j, broken_level)
        return None

    if sweep_kind == "low":
        opposing = [p for p in pivots if p[1] == "high" and p[0] < sweep_idx]
        if not opposing:
            log.debug("choch.no_opposing_high", extra={"sweep_idx": sweep_idx})
            return None
        broken_level = opposing[-1][2]
        for j in range(sweep_idx + 1, min(sweep_idx + 1 + lookforward, n)):
            if bars.iloc[j]["close"] > broken_level:
                return ("long", j, broken_level)
        return None

    return None


# ---------------------------------------------------------------------------
# End-to-end runner (returns dict for CrewAI tool usage)
# ---------------------------------------------------------------------------

def run_judas_detection(
    symbol: str,
    bars_df: pd.DataFrame,
    prior_high: float,
    prior_low: float,
    tick_size: float = 0.10,
    confirmation_bars: int = 4,
    pivot_length: int = 2,
    target_r: float = 2.0,
    stop_buffer_ticks: int = 2,
    min_sweep_ticks: int = 3,
) -> Optional[dict]:
    """Full Judas pipeline on 1H bars.

    Args:
        symbol: Instrument ticker (e.g. "MGC").
        bars_df: DataFrame with columns ts, open, high, low, close, volume.
                 Most recent bar is bars_df.iloc[-1].
        prior_high: Prior session high used as liquidity level.
        prior_low: Prior session low used as liquidity level.
        tick_size: Minimum price increment for the instrument.
        confirmation_bars: CHoCH must occur within this many bars after sweep.
        pivot_length: Bars required on each side of a swing pivot.
        target_r: Risk-multiple for profit target.
        stop_buffer_ticks: Extra ticks beyond sweep extreme for stop placement.
        min_sweep_ticks: Minimum ticks beyond level for a valid sweep.

    Returns:
        Dict with full setup details, or None if no valid setup found.
        Dict keys: symbol, direction, entry, stop, target, sweep_kind,
                   sweep_idx, sweep_extreme, sweep_ts, choch_idx, choch_ts,
                   broken_pivot, prior_high, prior_low, rationale.
    """
    if len(bars_df) < 2 * pivot_length + 2:
        log.debug("judas.insufficient_bars", extra={"symbol": symbol, "n": len(bars_df)})
        return None

    min_price_buffer = min_sweep_ticks * tick_size
    sweep = detect_sweep(
        bars_df,
        prior_high=prior_high,
        prior_low=prior_low,
        most_recent=True,
        min_ticks=min_price_buffer,
    )
    if sweep is None:
        log.debug("judas.no_sweep", extra={"symbol": symbol})
        return None

    kind, sweep_idx, sweep_extreme = sweep

    choch = detect_choch(
        bars_df,
        sweep_idx=sweep_idx,
        sweep_kind=kind,
        lookforward=confirmation_bars,
        pivot_len=pivot_length,
    )
    if choch is None:
        log.debug("judas.no_choch", extra={"symbol": symbol, "sweep_idx": sweep_idx})
        return None

    direction, choch_idx, broken_level = choch

    # Only fire on the most recent bar to avoid re-signalling stale setups
    if choch_idx != len(bars_df) - 1:
        log.debug(
            "judas.stale_choch",
            extra={"symbol": symbol, "choch_idx": choch_idx, "last_idx": len(bars_df) - 1},
        )
        return None

    entry = float(bars_df.iloc[choch_idx]["close"])
    buffer = stop_buffer_ticks * tick_size

    if direction == "short":
        stop = sweep_extreme + buffer
        risk = stop - entry
        if risk <= 0:
            log.warning("judas.invalid_geometry_short", extra={"symbol": symbol, "entry": entry, "stop": stop})
            return None
        target = entry - target_r * risk
    else:  # long
        stop = sweep_extreme - buffer
        risk = entry - stop
        if risk <= 0:
            log.warning("judas.invalid_geometry_long", extra={"symbol": symbol, "entry": entry, "stop": stop})
            return None
        target = entry + target_r * risk

    sweep_ts = str(bars_df.iloc[sweep_idx]["ts"])
    choch_ts = str(bars_df.iloc[choch_idx]["ts"])
    swept_level = prior_high if kind == "high" else prior_low
    rationale = (
        f"{symbol} swept prior {kind} ({swept_level:.4f}) at {sweep_ts} "
        f"[extreme={sweep_extreme:.4f}]; "
        f"CHoCH {direction} confirmed at {choch_ts} by closing through "
        f"swing {'low' if direction == 'short' else 'high'} {broken_level:.4f}. "
        f"Entry={entry:.4f} Stop={stop:.4f} Target={target:.4f} Risk={risk:.4f} "
        f"({confirmation_bars}-bar window, 1H timeframe)"
    )

    log.info("judas.signal", extra={
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "sweep_ts": sweep_ts,
        "choch_ts": choch_ts,
    })

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "sweep_kind": kind,
        "sweep_idx": sweep_idx,
        "sweep_extreme": sweep_extreme,
        "sweep_ts": sweep_ts,
        "choch_idx": choch_idx,
        "choch_ts": choch_ts,
        "broken_pivot": broken_level,
        "prior_high": prior_high,
        "prior_low": prior_low,
        "risk": risk,
        "target_r": target_r,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Rich enrichment: ATR, displacement, FVG, sweep type
# ---------------------------------------------------------------------------

def _calc_atr(bars_df: pd.DataFrame, period: int = 14) -> float:
    """14-bar ATR of the most recent bar (simplified — uses high-low range)."""
    if len(bars_df) < period:
        ranges = (bars_df["high"] - bars_df["low"]).values
    else:
        ranges = (bars_df["high"] - bars_df["low"]).values[-period:]
    return float(ranges.mean()) if len(ranges) > 0 else 0.0


def _avg_atr_20(bars_df: pd.DataFrame) -> float:
    """Average ATR over the last 20 bars (using high-low as proxy)."""
    n = min(20, len(bars_df))
    ranges = (bars_df["high"] - bars_df["low"]).values[-n:]
    return float(ranges.mean()) if len(ranges) > 0 else 0.0


def _displacement_strength(bars_df: pd.DataFrame, choch_idx: int) -> float:
    """CHoCH bar range / 20-bar average candle range."""
    choch_range = bars_df.iloc[choch_idx]["high"] - bars_df.iloc[choch_idx]["low"]
    avg_range = _avg_atr_20(bars_df)
    return float(choch_range / avg_range) if avg_range > 0 else 0.0


def _displacement_body_ratio(bars_df: pd.DataFrame, choch_idx: int) -> float:
    """Absolute candle body / total candle range for the CHoCH bar."""
    bar = bars_df.iloc[choch_idx]
    candle_range = float(bar["high"] - bar["low"])
    if candle_range <= 0:
        return 0.0
    return float(abs(bar["close"] - bar["open"]) / candle_range)


def _sweep_type(bars_df: pd.DataFrame, sweep_idx: int, sweep_kind: str,
                prior_high: float, prior_low: float) -> str:
    """'wick' if bar closed back inside the swept level, 'body' if close is also beyond."""
    bar = bars_df.iloc[sweep_idx]
    if sweep_kind == "high":
        # Wick = close is back inside (below prior_high), body = close is above
        return "wick" if bar["close"] <= prior_high else "body"
    else:
        # Wick = close is back inside (above prior_low), body = close is below
        return "wick" if bar["close"] >= prior_low else "body"


def _detect_fvg(bars_df: pd.DataFrame, sweep_idx: int) -> tuple[bool, float | None, float | None]:
    """Simplified FVG: gap between sweep bar close and next bar open.

    Returns (present, gap_high, gap_low).
    """
    if sweep_idx + 1 >= len(bars_df):
        return False, None, None
    sweep_close = bars_df.iloc[sweep_idx]["close"]
    next_open = bars_df.iloc[sweep_idx + 1]["open"]
    if abs(sweep_close - next_open) < 1e-8:
        return False, None, None
    gap_high = max(sweep_close, next_open)
    gap_low = min(sweep_close, next_open)
    return True, float(gap_high), float(gap_low)


def run_judas_detection_rich(
    symbol: str,
    bars_df: pd.DataFrame,
    prior_high: float,
    prior_low: float,
    tick_size: float = 0.10,
    confirmation_bars: int = 4,
    pivot_length: int = 2,
    target_r: float = 2.0,
    stop_buffer_ticks: int = 2,
    min_sweep_ticks: int = 3,
    dollar_per_point: float = 10.0,
) -> dict:
    """Full Judas pipeline with rich output matching PLAN.md spec.

    Returns the rich dict (pattern_found=True) or a minimal dict (pattern_found=False).
    Never returns None — always returns a dict for CrewAI JSON serialization.
    """
    current_atr = _calc_atr(bars_df, period=14)
    avg_atr_20 = _avg_atr_20(bars_df)
    atr_contracted = (current_atr < 0.5 * avg_atr_20) if avg_atr_20 > 0 else False

    base = {
        "pattern_found": False,
        "direction": None,
        "sweep": None,
        "choch": None,
        "displacement": None,
        "fvg": None,
        "atr_context": {
            "current_atr": current_atr,
            "avg_atr_20": avg_atr_20,
            "contracted": atr_contracted,
        },
        "stop_price": None,
        "target_price": None,
        "risk_dollars": None,
    }

    result = run_judas_detection(
        symbol=symbol,
        bars_df=bars_df,
        prior_high=prior_high,
        prior_low=prior_low,
        tick_size=tick_size,
        confirmation_bars=confirmation_bars,
        pivot_length=pivot_length,
        target_r=target_r,
        stop_buffer_ticks=stop_buffer_ticks,
        min_sweep_ticks=min_sweep_ticks,
    )

    if result is None:
        return base

    sweep_idx = result["sweep_idx"]
    choch_idx = result["choch_idx"]
    direction = result["direction"]
    sweep_kind = result["sweep_kind"]

    stype = _sweep_type(bars_df, sweep_idx, sweep_kind, prior_high, prior_low)
    fvg_present, fvg_high, fvg_low = _detect_fvg(bars_df, sweep_idx)
    disp_strength = _displacement_strength(bars_df, choch_idx)
    body_ratio = _displacement_body_ratio(bars_df, choch_idx)
    choch_range = bars_df.iloc[choch_idx]["high"] - bars_df.iloc[choch_idx]["low"]
    atr_ratio = float(choch_range / current_atr) if current_atr > 0 else 0.0

    risk_dollars = result["risk"] * dollar_per_point

    return {
        "pattern_found": True,
        "direction": direction,
        "sweep": {
            "type": stype,
            "bar_idx": sweep_idx,
            "extreme_price": result["sweep_extreme"],
            "prior_level_swept": sweep_kind,
            "prior_level_price": prior_high if sweep_kind == "high" else prior_low,
        },
        "choch": {
            "bar_idx": choch_idx,
            "broken_pivot": result["broken_pivot"],
            "entry_price": result["entry"],
        },
        "displacement": {
            "strength": round(disp_strength, 3),
            "atr_ratio": round(atr_ratio, 3),
            "body_ratio": round(body_ratio, 3),
        },
        "fvg": {
            "present": fvg_present,
            "gap_high": fvg_high,
            "gap_low": fvg_low,
        },
        "atr_context": {
            "current_atr": round(current_atr, 4),
            "avg_atr_20": round(avg_atr_20, 4),
            "contracted": atr_contracted,
        },
        "stop_price": result["stop"],
        "target_price": result["target"],
        "risk_dollars": round(risk_dollars, 2),
        "rationale": result["rationale"],
    }


# ---------------------------------------------------------------------------
# CrewAI @tool wrapper
# ---------------------------------------------------------------------------

import json as _json

from crewai.tools import tool as _tool


@_tool("judas_detector_tool")
def judas_detector_tool(input_json: str) -> str:
    """Run the deterministic Judas Swing detector on provided 1H bars.

    Input JSON:
    {
      "bars": [{"ts": "2026-05-08T14:00:00", "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}, ...],
      "prior_high": float,
      "prior_low": float,
      "symbol": "MGC"  (optional, defaults to "MGC")
    }

    Returns rich JSON dict matching PLAN.md judas_detector output spec:
    {
      "pattern_found": bool,
      "direction": "long" | "short" | null,
      "sweep": {...},
      "choch": {...},
      "displacement": {"strength": float, "atr_ratio": float},
      "fvg": {"present": bool, "gap_high": float|null, "gap_low": float|null},
      "atr_context": {"current_atr": float, "avg_atr_20": float, "contracted": bool},
      "stop_price": float | null,
      "target_price": float | null,
      "risk_dollars": float | null
    }
    """
    try:
        data = _json.loads(input_json) if isinstance(input_json, str) else input_json
        bars_raw = data.get("bars", [])
        prior_high = float(data.get("prior_high", 0.0))
        prior_low = float(data.get("prior_low", 0.0))
        symbol = data.get("symbol", "MGC").upper()
        strategy_params = (
            data.get("strategy_params")
            or (data.get("active_strategy") or {}).get("params")
            or {}
        )
    except (ValueError, KeyError, _json.JSONDecodeError) as e:
        return _json.dumps({"error": f"Invalid input: {e}", "pattern_found": False})

    if not bars_raw:
        return _json.dumps({"error": "No bars provided", "pattern_found": False})

    bars_df = pd.DataFrame(bars_raw)
    required_cols = {"open", "high", "low", "close"}
    missing = required_cols - set(bars_df.columns)
    if missing:
        return _json.dumps({"error": f"Missing columns: {missing}", "pattern_found": False})

    if "ts" not in bars_df.columns:
        bars_df["ts"] = range(len(bars_df))

    # Symbol-specific params — derived from portfolio_runtime._CONTRACT_SPECS
    # (tick_value / tick = dollar_per_point).  Missing symbols defaulted to
    # MGC-like values which gave near-zero dollar P&L on 6J/DX/ZF etc.
    _tick_map = {
        "MGC": 0.10, "MNQ": 0.25,
        "MCL": 0.01, "MBT": 5.0, "MET": 0.50,
        "DX": 0.005, "ZF": 0.0078125, "6J": 0.0000005,
    }
    _dpp_map = {
        "MGC": 10.0, "MNQ": 2.0,
        "MCL": 100.0, "MBT": 0.1, "MET": 0.1,
        "DX": 1_000.0, "ZF": 1_000.0, "6J": 12_500_000.0,
    }
    tick_size = float(strategy_params.get("tick_size", _tick_map.get(symbol, 0.10)))
    dollar_per_point = _dpp_map.get(symbol, 10.0)

    result = run_judas_detection_rich(
        symbol=symbol,
        bars_df=bars_df,
        prior_high=prior_high,
        prior_low=prior_low,
        tick_size=tick_size,
        confirmation_bars=int(strategy_params.get("confirmation_bars", 4)),
        pivot_length=int(strategy_params.get("pivot_length", 2)),
        target_r=float(strategy_params.get("target_r", 2.0)),
        stop_buffer_ticks=int(strategy_params.get("stop_buffer_ticks", 2)),
        min_sweep_ticks=int(strategy_params.get("min_sweep_ticks", 3)),
        dollar_per_point=dollar_per_point,
    )
    if strategy_params:
        result["strategy_params"] = strategy_params
    return _json.dumps(result, default=str)
