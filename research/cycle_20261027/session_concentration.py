"""SESSION CONCENTRATION OF THE MGC EDGE (2026-10-27 cycle).

QUESTION
--------
The market-entry sweep->displacement architecture (csid=439) is the only
family in this book that survived the honest-fill audit AND a TRUE
out-of-sample holdout, and it is profitable on MGC 15m (live PF 1.32-1.72
depending on the cell).  Its cross-symbol port FAILS on MNQ/MCL/ZF/6J even
after a placeholder-bar hygiene guard (finding #9151).

If the edge is a SESSION phenomenon (e.g. the NY cash open, or the London
fix), then the same session gate applied to the other symbols might work.
This script measures WHERE (UTC hour) the MGC edge lives and whether any
hour bucket is profitable on the other four legal symbols.

FILLS: the `live` book -- fill at the OPEN of the bar AFTER the decision bar,
brackets re-anchored, gap-aware exits (production semantics, finding #9134).

Usage:
  .venv/bin/python research/cycle_20261027/session_concentration.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from honest_screen import load_bars, metrics, run_books  # noqa: E402
from src import bar_archive  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261027" / "strategies" /
        "market_entry_sweep_cont_hygiene_v1.py").read_text()

CELL = {"lookback": 8, "min_sweep_atr": 0.45, "disp_k": 1.0, "rr": 1.5,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}

SYMBOLS = ["MGC", "MNQ", "MCL", "ZF", "6J"]


def clean_bars(symbol: str, tf: str = "15m", days: int = 240):
    df = bar_archive.read_archive(symbol, tf)
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    ok = ((df["high"] - df["low"]) > 0) & (df["volume"] > 0)
    df = df[ok].reset_index(drop=True)
    return df


def pf(trades):
    if not trades:
        return 0.0, 0, 0.0
    win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return (win / loss if loss > 0 else 0.0), len(trades), sum(t["pnl"] for t in trades)


def main():
    out = {}
    for sym in SYMBOLS:
        bars = clean_bars(sym)
        if bars is None or len(bars) < 500:
            print(f"{sym}: no archive")
            continue
        ts = bars["ts"]
        base_t, live_t, fair_t, meta = run_books(CODE, bars, CELL, sym)
        if not live_t:
            print(f"{sym}: no live trades ({meta})")
            continue
        hours = ts.dt.hour.to_numpy()
        buckets = {}
        for t in live_t:
            h = int(hours[t["entry_bar"]])
            buckets.setdefault(h, []).append(t)
        rows = []
        for h in sorted(buckets):
            p_, n_, tot = pf(buckets[h])
            rows.append({"hour": h, "n": n_, "pf": round(p_, 3), "pnl": round(tot, 2)})
        allp, alln, alltot = pf(live_t)
        out[sym] = {"n": alln, "pf": round(allp, 3), "pnl": round(alltot, 2),
                    "by_hour": rows}
        print(f"== {sym}  ALL live: n={alln} pf={allp:.3f} pnl={alltot:,.0f}")
        for r in rows:
            flag = "  <<<" if r["pf"] >= 1.25 and r["n"] >= 6 else ""
            print(f"   h={r['hour']:02d} n={r['n']:3d} pf={r['pf']:.3f} pnl={r['pnl']:>10,.0f}{flag}")
    (ROOT / "outputs" / "session_concentration_20261027.json").write_text(
        json.dumps(out, indent=2))
    print("\nwrote outputs/session_concentration_20261027.json")


if __name__ == "__main__":
    main()
