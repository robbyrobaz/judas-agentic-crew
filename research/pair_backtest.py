"""
Pair-trade backtest — mirrors _evaluate_pair in src/portfolio_runtime.py.

Loads cached 1h bars for two symbols, computes rolling-beta log-spread z-score,
simulates pair entries when |z| > z_entry, exits when stop or target hit.

Usage:
    python research/pair_backtest.py --primary MGC --hedge MNQ \
        --window 100 --z-entry 2.0 --z-exit 0.5 --z-stop 4.5 --days 90

Outputs PF, win-rate, E[R], trade count. Prints JSON.
"""
import argparse
import json
import sys
from pathlib import Path

# Repo-relative imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.bar_cache import read_cache


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-style ATR matching portfolio_runtime._atr."""
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def backtest_pair(
    *,
    primary: str,
    hedge: str,
    window: int = 100,
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    z_stop: float = 4.5,
    days: int = 90,
    timeframe: str = "1h",
) -> dict:
    """Simulate the live _evaluate_pair logic on cached bars."""
    bp = read_cache(primary, timeframe).copy()
    bh = read_cache(hedge, timeframe).copy()
    if bp is None or bh is None or len(bp) < window + 5 or len(bh) < window + 5:
        return {"error": "insufficient bars", "primary": primary, "hedge": hedge}

    # Ensure ts column and align on common timestamps
    if "ts" in bp.columns:
        bp["ts"] = pd.to_datetime(bp["ts"], utc=True)
        bp = bp.set_index("ts")
    if "ts" in bh.columns:
        bh["ts"] = pd.to_datetime(bh["ts"], utc=True)
        bh = bh.set_index("ts")

    # Trim to days
    per_day = {"5m": 288, "15m": 96, "1h": 24}.get(timeframe, 24)
    if days and len(bp) > per_day * days:
        bp = bp.tail(per_day * days)
        bh = bh.tail(per_day * days)

    df = pd.concat({
        "a_close": bp["close"],
        "b_close": bh["close"],
    }, axis=1).dropna()
    if (df["a_close"] <= 0).any() or (df["b_close"] <= 0).any():
        return {"error": "non-positive prices"}
    if len(df) < window + 5:
        return {"error": "not enough aligned bars"}

    log_a = np.log(df["a_close"])
    log_b = np.log(df["b_close"])
    beta = (log_a.rolling(window).cov(log_b) / log_b.rolling(window).var()).fillna(1.0)
    spread = log_a - beta * log_b
    mu = spread.rolling(window).mean()
    sd = spread.rolling(window).std()
    z = (spread - mu) / sd

    df = df.assign(z=z).dropna(subset=["z"])
    if df.empty:
        return {"error": "no valid z-score bars"}

    # Pre-compute per-symbol ATR (used for stops/targets)
    atr_a = _atr(bp).reindex(df.index)
    atr_b = _atr(bh).reindex(df.index)
    df = df.assign(atr_a=atr_a, atr_b=atr_b)

    # Walk forward — when |z| > z_entry, open a pair trade; exit when stop/target hit
    n = len(df)
    closes_a = df["a_close"].values
    closes_b = df["b_close"].values
    highs_a = bp["high"].reindex(df.index).values
    lows_a = bp["low"].reindex(df.index).values
    highs_b = bh["high"].reindex(df.index).values
    lows_b = bh["low"].reindex(df.index).values
    atr_a = df["atr_a"].values
    atr_b = df["atr_b"].values
    z_arr = df["z"].values

    open_trade = None
    pnls_a: list[float] = []
    pnls_b: list[float] = []
    rs: list[float] = []

    for i in range(1, n):
        # Resolve open trade against current bar's high/low
        if open_trade is not None:
            t = open_trade
            hit_stop_a = (t["dir_a"] == "long" and lows_a[i] <= t["stop_a"]) or \
                         (t["dir_a"] == "short" and highs_a[i] >= t["stop_a"])
            hit_tgt_a = (t["dir_a"] == "long" and highs_a[i] >= t["tgt_a"]) or \
                        (t["dir_a"] == "short" and lows_a[i] <= t["tgt_a"])
            hit_stop_b = (t["dir_b"] == "long" and lows_b[i] <= t["stop_b"]) or \
                         (t["dir_b"] == "short" and highs_b[i] >= t["stop_b"])
            hit_tgt_b = (t["dir_b"] == "long" and highs_b[i] >= t["tgt_b"]) or \
                        (t["dir_b"] == "short" and lows_b[i] <= t["tgt_b"])

            # Per-leg exit (each leg exits on its own — mirror IBKR behaviour)
            for leg_id, hit, hit_t, entry, stop, tgt, dir_ in [
                ("a", hit_stop_a, hit_tgt_a, t["entry_a"], t["stop_a"], t["tgt_a"], t["dir_a"]),
                ("b", hit_stop_b, hit_tgt_b, t["entry_b"], t["stop_b"], t["tgt_b"], t["dir_b"]),
            ]:
                if not (hit or hit_t):
                    continue
                # Use worst-of priority: stop checked first when both hit
                if hit:
                    pnl = -abs(entry - stop)
                else:
                    pnl = abs(tgt - entry)
                # Direction sign: short profits when target < entry
                if dir_ == "short":
                    pnl = -pnl if hit else pnl
                if leg_id == "a":
                    pnls_a.append(pnl)
                else:
                    pnls_b.append(pnl)

            # Close pair if either leg hit
            if hit_stop_a or hit_tgt_a or hit_stop_b or hit_tgt_b:
                rs.append(((pnls_a[-1] if pnls_a else 0) + (pnls_b[-1] if pnls_b else 0)) /
                          max(abs(t["entry_a"] - t["stop_a"]), abs(t["entry_b"] - t["stop_b"]), 1e-9))
                open_trade = None
                continue

        if open_trade is not None:
            continue

        # Entry: |z| > z_entry
        z_now = z_arr[i]
        if not np.isfinite(z_now) or abs(z_now) < z_entry:
            continue

        entry_a = closes_a[i]
        entry_b = closes_b[i]
        atr_a_i = atr_a[i] if np.isfinite(atr_a[i]) else abs(entry_a - closes_a[i - 1])
        atr_b_i = atr_b[i] if np.isfinite(atr_b[i]) else abs(entry_b - closes_b[i - 1])

        if z_now <= -z_entry:
            # Spread is below mean: long A, short B
            dir_a, dir_b = "long", "short"
        else:
            dir_a, dir_b = "short", "long"

        if dir_a == "long":
            stop_a = entry_a - atr_a_i
            tgt_a = entry_a + 1.5 * atr_a_i
        else:
            stop_a = entry_a + atr_a_i
            tgt_a = entry_a - 1.5 * atr_a_i
        if dir_b == "long":
            stop_b = entry_b - atr_b_i
            tgt_b = entry_b + 1.5 * atr_b_i
        else:
            stop_b = entry_b + atr_b_i
            tgt_b = entry_b - 1.5 * atr_b_i

        open_trade = dict(
            dir_a=dir_a, dir_b=dir_b,
            entry_a=entry_a, entry_b=entry_b,
            stop_a=stop_a, stop_b=stop_b,
            tgt_a=tgt_a, tgt_b=tgt_b,
            entry_z=z_now, idx=i,
        )

    n_trades = len(pnls_a)  # 1 trade = pair event (both legs close together)
    total_a = float(np.sum(pnls_a)) if pnls_a else 0.0
    total_b = float(np.sum(pnls_b)) if pnls_b else 0.0
    total_pnl = total_a + total_b
    wins = sum(1 for p in pnls_a if p > 0)
    losses = sum(1 for p in pnls_a if p <= 0)
    avg_win = float(np.mean([p for p in pnls_a if p > 0])) if wins else 0.0
    avg_loss = float(np.mean([p for p in pnls_a if p <= 0])) if losses else 0.0
    pf = (wins * avg_win) / abs(losses * avg_loss) if losses and avg_loss else float("inf")
    er = float(np.mean(rs)) if rs else 0.0
    cum = np.cumsum([a + b for a, b in zip(pnls_a, pnls_b)])
    peak = np.maximum.accumulate(cum) if len(cum) else cum
    max_dd = float(np.min(cum - peak)) if len(cum) else 0.0

    return {
        "primary": primary,
        "hedge": hedge,
        "window": window,
        "z_entry": z_entry,
        "z_exit": z_exit,
        "z_stop": z_stop,
        "days": days,
        "timeframe": timeframe,
        "n_trades": int(n_trades),
        "n_wins": int(wins),
        "n_losses": int(losses),
        "win_rate": float(wins / n_trades) if n_trades else 0.0,
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "pf": float(pf) if pf != float("inf") else 999.0,
        "expectancy_r": float(er),
        "total_pnl": float(total_pnl),
        "total_pnl_a": float(total_a),
        "total_pnl_b": float(total_b),
        "max_drawdown": float(max_dd),
        "bars_evaluated": int(n),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--primary", required=True)
    p.add_argument("--hedge", required=True)
    p.add_argument("--window", type=int, default=100)
    p.add_argument("--z-entry", type=float, default=2.0)
    p.add_argument("--z-exit", type=float, default=0.5)
    p.add_argument("--z-stop", type=float, default=4.5)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--timeframe", default="1h")
    args = p.parse_args()

    res = backtest_pair(
        primary=args.primary,
        hedge=args.hedge,
        window=args.window,
        z_entry=args.z_entry,
        z_exit=args.z_exit,
        z_stop=args.z_stop,
        days=args.days,
        timeframe=args.timeframe,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()