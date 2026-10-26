"""Fast local simulator that MIRRORS src/research/custom_strategy_runtime.py
exactly, but with vectorized signal generation.

Why this exists: the production engine calls evaluate(bars[:i+1]) once per
bar, which is O(n^2) in pandas and costs ~50s per 120-day 15m run. That is
too slow to explore a parameter grid. This module reproduces the engine's
trade semantics 1:1 (including the stop-before-target tie-break, the
entry-at-signal-bar-close fill, no re-entry while a trade is open, and the
v1_realistic_micros cost model) so exploration is free.

FINAL evidence is ALWAYS re-confirmed through the official
run_custom_backtest tool before anything is proposed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION, apply_costs_to_trade  # noqa: E402


def load_bars(symbol: str, tf: str, days: int) -> pd.DataFrame:
    p = Path("data/bars_archive") / f"{symbol}_{tf}.parquet"
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    per_day = {"5m": 288, "15m": 96, "1h": 24}[tf]
    if days and len(df) > per_day * days:
        df = df.tail(per_day * days).reset_index(drop=True)
    return df


def simulate(bars: pd.DataFrame, sigs: dict, symbol: str, qty: int = 1) -> dict:
    """sigs: {bar_index: (direction, entry, stop, target)}"""
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    spec = _CONTRACT_SPECS.get(symbol)
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    n = len(bars)
    pnls: list[float] = []
    rs: list[float] = []
    wins = losses = 0
    n_signals = 0
    open_trade = None
    for i in range(2, n):
        if open_trade is not None:
            direction, entry, stop, target = open_trade
            if direction == "long":
                hit_stop = l[i] <= stop
                hit_target = h[i] >= target
            else:
                hit_stop = h[i] >= stop
                hit_target = l[i] <= target
            if hit_stop or hit_target:
                risk = abs(entry - stop) or 1.0
                target_won = hit_target and not hit_stop
                points = abs(target - entry) if target_won else -abs(entry - stop)
                gross = points * dpp * qty
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason="target" if target_won else "stop",
                                           qty=qty)
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                pnls.append(float(pnl))
                rs.append(float(pnl / (risk * dpp * qty)))
                open_trade = None
                continue
            continue
        sig = sigs.get(i)
        if sig is None:
            continue
        n_signals += 1
        open_trade = sig
    total = float(sum(pnls))
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = (gw / gl) if gl > 0 else (0.0 if gw == 0 else 999.0)
    running = peak = dd = 0.0
    for x in pnls:
        running += x
        peak = max(peak, running)
        dd = max(dd, peak - running)
    ts = bars["ts"]
    return {
        "n_trades": len(pnls), "n_signals": n_signals, "n_wins": wins, "n_losses": losses,
        "pf": pf, "expectancy_r": float(np.mean(rs)) if rs else 0.0,
        "total_pnl": total, "max_drawdown": dd,
        "winrate": 100.0 * wins / max(1, len(pnls)),
        "cost_model": COST_MODEL_VERSION, "qty": qty, "symbol": symbol,
        "bars_span_days": int((ts.iloc[-1] - ts.iloc[0]).total_seconds() // 86400),
        "first_bar": str(ts.iloc[0]), "last_bar": str(ts.iloc[-1]),
        "dollars_per_point": dpp,
    }


def fmt(m: dict) -> str:
    return (f"PF={m['pf']:.3f} n={m['n_trades']:4d} WR={m['winrate']:5.1f}% "
            f"E[R]={m['expectancy_r']:+.3f} ${m['total_pnl']:+9.2f} DD=${m['max_drawdown']:8.2f}")
