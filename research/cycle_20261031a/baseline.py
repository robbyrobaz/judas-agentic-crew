"""Baseline diagnostic: the surviving market-entry family (csid439 semantics)
on every legal symbol x timeframe, PRODUCTION fill, with a chronological
train/test split and the cost-drag diagnostic.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
from explore_prod import LEGAL, load_bars, sigs_sweep_disp, simulate_prod, split_report  # noqa: E402

BASE = {"lookback": 12, "min_sweep_atr": 0.30, "disp_k": 1.0, "rr": 1.5,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}

rows = []
for sym in LEGAL:
    for tf in ("15m", "5m"):
        try:
            bars = load_bars(sym, tf, None)
        except Exception as exc:  # noqa: BLE001
            print(f"{sym} {tf}: load failed {exc}")
            continue
        if len(bars) < 2000:
            print(f"{sym} {tf}: only {len(bars)} bars")
            continue
        p = dict(BASE, _tf=tf)
        sigs = sigs_sweep_disp(bars, p)
        rep = split_report(bars, sigs, sym, frac=0.6)
        full = simulate_prod(bars, sigs, sym)
        rows.append({
            "sym": sym, "tf": tf, "bars": len(bars),
            "start": str(bars["ts"].iloc[0])[:10], "end": str(bars["ts"].iloc[-1])[:10],
            "tr_n": rep["train"]["n_trades"], "tr_pf": rep["train"]["pf"],
            "tr_er": rep["train"]["expectancy_r"],
            "te_n": rep["test"]["n_trades"], "te_pf": rep["test"]["pf"],
            "te_er": rep["test"]["expectancy_r"],
            "full_n": full["n_trades"], "full_pf": full["pf"],
            "full_pnl": full["total_pnl"], "risk$": full["avg_risk_dollars"],
            "costR": full["cost_in_r"],
        })
        print(rows[-1])

df = pd.DataFrame(rows)
df.to_csv("research/cycle_20261031a/baseline_prod.csv", index=False)
print(df.to_string(index=False))
