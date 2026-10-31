"""Coordinate search over the market-entry sweep->displacement family.

Discipline (to avoid manufacturing an edge):
  * ONE axis is changed at a time from the csid439 baseline;
  * every config is scored by a 3-fold CHRONOLOGICAL walk-forward:
        fold 1: train [0,40%)  test [40,60%)
        fold 2: train [0,60%)  test [60,80%)
        fold 3: train [0,80%)  test [80,100%)
    the score is the WORST test PF plus the mean test PF -- a config only
    passes if it is positive in EVERY out-of-sample fold;
  * the winner must ALSO be confirmed by the official run_custom_backtest
    tool (production fill, v1_realistic_micros) before it is proposed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from explore_prod import load_bars, sigs_sweep_disp, simulate_prod  # noqa: E402

BASE = {"lookback": 12, "min_sweep_atr": 0.30, "disp_k": 1.0, "rr": 1.5,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14, "stop_mode": "structural", "sweep_ref": "rolling",
        "sess_lo": -1, "sess_hi": -1, "htf_mult": 0, "htf_ema": 20,
        "stop_atr_mult": 1.0, "long_only": 0, "short_only": 0}

FOLDS = [(0.40, 0.20), (0.60, 0.20), (0.80, 0.20)]


def wf_score(bars: pd.DataFrame, sigs: dict, symbol: str) -> dict:
    n = len(bars)
    res = {"folds": []}
    pfs = []
    for (tr_end, size) in FOLDS:
        a = int(n * tr_end)
        b = min(int(n * (tr_end + size)), n)
        sub = bars.iloc[a:b].reset_index(drop=True)
        ss = {i - a: v for i, v in sigs.items() if a <= i < b}
        r = simulate_prod(sub, ss, symbol)
        pfs.append(r["pf"])
        res["folds"].append({"test": [str(bars["ts"].iloc[a])[:10], str(bars["ts"].iloc[b - 1])[:10]],
                             "n": r["n_trades"], "pf": r["pf"], "er": r["expectancy_r"],
                             "pnl": r["total_pnl"]})
    res["min_pf"] = round(float(min(pfs)), 4) if pfs else 0.0
    res["mean_pf"] = round(float(np.mean(pfs)), 4) if pfs else 0.0
    res["all_pos"] = bool(pfs and all(p > 1.0 for p in pfs))
    res["n_total"] = sum(f["n"] for f in res["folds"])
    return res


AXES = {
    "lookback": [8, 12, 20, 30, 45],
    "min_sweep_atr": [0.15, 0.30, 0.45, 0.60],
    "disp_k": [0.6, 0.8, 1.0, 1.25, 1.5],
    "confirm_bars": [2, 3, 6, 10, 16],
    "rr": [1.0, 1.5, 2.0, 3.0, 4.0],
    "body_ratio_min": [0.35, 0.45, 0.55, 0.65, 0.75],
    "stop_mode": ["structural", "atr"],
    "stop_atr_mult": [0.6, 1.0, 1.5, 2.0],
    "stop_buf_atr": [0.05, 0.15, 0.30, 0.60],
    "sweep_ref": ["rolling", "pdh_pdl"],
    "session": [(-1, -1), (6, 12), (7, 12), (8, 12), (12, 21), (13, 20),
                (14, 18), (7, 16), (12, 18), (6, 20), (0, 24)],
    "htf_mult": [0, 2, 4, 8],
    "htf_ema": [10, 20, 40],
    "long_only": [0, 1],
    "short_only": [0, 1],
}


def apply_axis(cfg: dict, axis: str, value) -> dict:
    c = dict(cfg)
    if axis == "session":
        c["sess_lo"], c["sess_hi"] = value
    else:
        c[axis] = value
    return c


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    bars = load_bars(symbol, tf, None)
    print(f"# {symbol} {tf}  bars={len(bars)}  "
          f"{bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}")
    out = {}
    cur = dict(BASE)
    base_sigs = sigs_sweep_disp(bars, dict(cur, _tf=tf))
    base = wf_score(bars, base_sigs, symbol)
    out["__base__"] = base
    print("BASE", json.dumps(base))

    results = []
    for axis, values in AXES.items():
        for v in values:
            cfg = apply_axis(cur, axis, v)
            if axis != "session" and axis in cur and cur[axis] == v:
                continue
            if axis == "session" and (cur["sess_lo"], cur["sess_hi"]) == tuple(v):
                continue
            sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
            sc = wf_score(bars, sigs, symbol)
            sc["axis"] = axis
            sc["value"] = v
            sc["cfg"] = {k: cfg[k] for k in cfg}
            results.append(sc)
            flag = "PASS" if sc["all_pos"] and sc["n_total"] >= 30 else "    "
            print(f"{flag} {axis:>15}={str(v):>10}  minPF={sc['min_pf']:.3f} "
                  f"meanPF={sc['mean_pf']:.3f} n={sc['n_total']:4d} "
                  f"folds={[f['pf'] for f in sc['folds']]}")
    with open(f"research/cycle_20261031a/coord_{symbol}_{tf}.json", "w") as fh:
        json.dump(results, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
