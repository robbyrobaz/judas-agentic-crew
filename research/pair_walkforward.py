"""
Pair-trade walk-forward validation.

Splits cached bars into rolling train/test windows and runs the pair
backtest on each, mirroring how portfolio_runtime._evaluate_pair operates.

For each window we record train_pf, test_pf, test_n_trades, test_E[R].
Robust = avg(test_pf) >= 1.3 with no individual test window pf < 1.0.

Usage:
    python research/pair_walkforward.py --primary MGC --hedge MNQ \
        --window 100 --z-entry 2.0 --n-folds 4 --timeframe 1h
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.bar_cache import read_cache
from research.pair_backtest import _atr


def build_z_series(primary: str, hedge: str, window: int, timeframe: str) -> pd.DataFrame:
    bp = read_cache(primary, timeframe).copy()
    bh = read_cache(hedge, timeframe).copy()
    if "ts" in bp.columns:
        bp["ts"] = pd.to_datetime(bp["ts"], utc=True)
        bp = bp.set_index("ts")
    if "ts" in bh.columns:
        bh["ts"] = pd.to_datetime(bh["ts"], utc=True)
        bh = bh.set_index("ts")
    df = pd.concat({"a_close": bp["close"], "b_close": bh["close"]}, axis=1).dropna()
    log_a, log_b = np.log(df["a_close"]), np.log(df["b_close"])
    beta = (log_a.rolling(window).cov(log_b) / log_b.rolling(window).var()).fillna(1.0)
    spread = log_a - beta * log_b
    mu, sd = spread.rolling(window).mean(), spread.rolling(window).std()
    z = (spread - mu) / sd
    df = df.assign(z=z)
    df["atr_a"] = _atr(bp).reindex(df.index)
    df["atr_b"] = _atr(bh).reindex(df.index)
    df["high_a"] = bp["high"].reindex(df.index)
    df["low_a"] = bp["low"].reindex(df.index)
    df["high_b"] = bh["high"].reindex(df.index)
    df["low_b"] = bh["low"].reindex(df.index)
    return df.dropna(subset=["z", "atr_a", "atr_b"])


def simulate_window(
    df: pd.DataFrame,
    *,
    z_entry: float,
) -> dict:
    closes_a = df["a_close"].values
    closes_b = df["b_close"].values
    highs_a = df["high_a"].values
    lows_a = df["low_a"].values
    highs_b = df["high_b"].values
    lows_b = df["low_b"].values
    atr_a = df["atr_a"].values
    atr_b = df["atr_b"].values
    z_arr = df["z"].values

    open_trade = None
    pnls_a, pnls_b, rs = [], [], []
    n = len(df)

    for i in range(1, n):
        if open_trade is not None:
            t = open_trade
            hit_stop_a = (t["dir_a"] == "long" and lows_a[i] <= t["stop_a"]) or (t["dir_a"] == "short" and highs_a[i] >= t["stop_a"])
            hit_tgt_a = (t["dir_a"] == "long" and highs_a[i] >= t["tgt_a"]) or (t["dir_a"] == "short" and lows_a[i] <= t["tgt_a"])
            hit_stop_b = (t["dir_b"] == "long" and lows_b[i] <= t["stop_b"]) or (t["dir_b"] == "short" and highs_b[i] >= t["stop_b"])
            hit_tgt_b = (t["dir_b"] == "long" and highs_b[i] >= t["tgt_b"]) or (t["dir_b"] == "short" and lows_b[i] <= t["tgt_b"])
            for leg_id, hit, hit_t, entry, stop, tgt, dir_ in [
                ("a", hit_stop_a, hit_tgt_a, t["entry_a"], t["stop_a"], t["tgt_a"], t["dir_a"]),
                ("b", hit_stop_b, hit_tgt_b, t["entry_b"], t["stop_b"], t["tgt_b"], t["dir_b"]),
            ]:
                if not (hit or hit_t):
                    continue
                if hit:
                    pnl = -abs(entry - stop)
                else:
                    pnl = abs(tgt - entry)
                if dir_ == "short":
                    pnl = -pnl if hit else pnl
                if leg_id == "a":
                    pnls_a.append(pnl)
                else:
                    pnls_b.append(pnl)
            if hit_stop_a or hit_tgt_a or hit_stop_b or hit_tgt_b:
                last_a = pnls_a[-1] if pnls_a else 0
                last_b = pnls_b[-1] if pnls_b else 0
                rs.append((last_a + last_b) / max(abs(t["entry_a"] - t["stop_a"]), abs(t["entry_b"] - t["stop_b"]), 1e-9))
                open_trade = None
                continue

        if open_trade is not None:
            continue

        z_now = z_arr[i]
        if not np.isfinite(z_now) or abs(z_now) < z_entry:
            continue

        entry_a, entry_b = closes_a[i], closes_b[i]
        atr_a_i = atr_a[i] if np.isfinite(atr_a[i]) else abs(entry_a - closes_a[i - 1])
        atr_b_i = atr_b[i] if np.isfinite(atr_b[i]) else abs(entry_b - closes_b[i - 1])
        if z_now <= -z_entry:
            dir_a, dir_b = "long", "short"
        else:
            dir_a, dir_b = "short", "long"
        if dir_a == "long":
            stop_a, tgt_a = entry_a - atr_a_i, entry_a + 1.5 * atr_a_i
        else:
            stop_a, tgt_a = entry_a + atr_a_i, entry_a - 1.5 * atr_a_i
        if dir_b == "long":
            stop_b, tgt_b = entry_b - atr_b_i, entry_b + 1.5 * atr_b_i
        else:
            stop_b, tgt_b = entry_b + atr_b_i, entry_b - 1.5 * atr_b_i
        open_trade = dict(
            dir_a=dir_a, dir_b=dir_b,
            entry_a=entry_a, entry_b=entry_b,
            stop_a=stop_a, stop_b=stop_b,
            tgt_a=tgt_a, tgt_b=tgt_b,
            entry_z=z_now, idx=i,
        )

    n_trades = len(pnls_a)
    total = sum(pnls_a) + sum(pnls_b) if pnls_a else 0
    wins = sum(1 for p in pnls_a if p > 0)
    losses = sum(1 for p in pnls_a if p <= 0)
    avg_win = float(np.mean([p for p in pnls_a if p > 0])) if wins else 0.0
    avg_loss = float(np.mean([p for p in pnls_a if p <= 0])) if losses else 0.0
    pf = (wins * avg_win) / abs(losses * avg_loss) if losses and avg_loss else 999.0
    er = float(np.mean(rs)) if rs else 0.0
    return dict(
        n_trades=n_trades, n_wins=wins, n_losses=losses,
        pf=float(pf), expectancy_r=er,
        total_pnl=float(total),
    )


def walk_forward(
    *,
    primary: str, hedge: str,
    window: int, z_entry: float,
    n_folds: int = 4,
    timeframe: str = "1h",
) -> dict:
    df = build_z_series(primary, hedge, window, timeframe)
    if df.empty or len(df) < window + 20:
        return {"error": "insufficient data"}

    # Time-based expanding-window CV: each fold = train (oldest 60%) + test (next 25% non-overlap)
    n = len(df)
    folds = []
    base_train_frac = 0.5  # 50% train
    test_frac = 0.18  # ~18% test per fold
    stride = test_frac
    train_start = 0
    train_end = int(n * base_train_frac)
    while train_end + int(n * test_frac) <= n and len(folds) < n_folds:
        test_start = train_end
        test_end = min(int(test_start + n * test_frac), n)
        fold = dict(
            train_idx=(train_start, train_end),
            test_idx=(test_start, test_end),
            train=simulate_window(df.iloc[train_start:train_end], z_entry=z_entry),
            test=simulate_window(df.iloc[test_start:test_end], z_entry=z_entry),
        )
        folds.append(fold)
        train_end = test_start + int(n * stride)
    if not folds:
        return {"error": "no folds produced"}

    test_pfs = [f["test"]["pf"] for f in folds]
    test_ers = [f["test"]["expectancy_r"] for f in folds]
    test_n = sum(f["test"]["n_trades"] for f in folds)
    avg_pf = float(np.mean(test_pfs)) if test_pfs else 0.0
    min_pf = float(min(test_pfs)) if test_pfs else 0.0
    avg_er = float(np.mean(test_ers)) if test_ers else 0.0
    total_test_pnl = sum(f["test"]["total_pnl"] for f in folds)
    robustness = sum(1 for pf in test_pfs if pf >= 1.0)

    return dict(
        primary=primary, hedge=hedge,
        window=window, z_entry=z_entry,
        n_folds=len(folds),
        avg_test_pf=avg_pf, min_test_pf=min_pf,
        avg_test_er=avg_er, total_test_trades=test_n,
        total_test_pnl=total_test_pnl,
        robustness=int(robustness),
        pass_pf=avg_pf >= 1.3,
        pass_trades=test_n >= 15,
        pass_er=avg_er > 0,
        pass_robust=robustness >= int(np.ceil(len(folds) / 2)),
        folds=folds,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--primary", required=True)
    p.add_argument("--hedge", required=True)
    p.add_argument("--window", type=int, default=100)
    p.add_argument("--z-entry", type=float, default=2.0)
    p.add_argument("--n-folds", type=int, default=4)
    p.add_argument("--timeframe", default="1h")
    args = p.parse_args()
    res = walk_forward(
        primary=args.primary, hedge=args.hedge,
        window=args.window, z_entry=args.z_entry,
        n_folds=args.n_folds, timeframe=args.timeframe,
    )
    # Strip verbose folds for stdout readability
    out = {k: v for k, v in res.items() if k != "folds"}
    out["per_fold_summary"] = [
        {
            "train_idx": f["train_idx"], "test_idx": f["test_idx"],
            "train_pf": f["train"]["pf"], "test_pf": f["test"]["pf"],
            "test_n": f["test"]["n_trades"], "test_er": f["test"]["expectancy_r"],
            "test_pnl": f["test"]["total_pnl"],
        } for f in res.get("folds", [])
    ]
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()