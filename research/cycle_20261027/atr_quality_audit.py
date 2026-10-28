"""ATR-QUALITY AUDIT: does candidate #7382's TRUE OOS holdout survive a
real-bar ATR?

WHY
---
Candidate #7382 (MGC, sweep_cont_market_entry_15m, csid=439) is the only
candidate in the book validated on a TRUE out-of-sample holdout: PF 1.452,
n=90 over 2025-11-12 -> 2026-06-10, never seen by the grid search.

But the MGC 15m deep archive for that window is ~58% NO-TRADE BARS (IBKR
returns high==low==open==close when it has nothing to report inside the
interval).  A 0-range bar contributes TR=0 to the ATR, so the ATR is
understated where the data is thin, and EVERY threshold in the family is in
ATR units.  The holdout PF may therefore be partly a data-quality artefact.

This script runs the EXACT #7382 configuration four ways over the SAME
windows and prints them side by side:

  A. all bars, ATR from all bars         <- what #7382 claims
  B. all bars, ATR from REAL bars only   <- same bar continuity, clean ATR
  C. REAL bars only (no-trade bars dropped), ATR from real bars
     (breaks time continuity -> reported with a gap diagnostic)
  D. all bars, real-bar ATR, for reference

The question is not "does B look better" -- it is "does A's edge SURVIVE B".
If A's PF 1.452 collapses under B, the holdout claim is not safe to promote.

Usage:
  .venv/bin/python research/cycle_20261027/atr_quality_audit.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import metrics, run_books  # noqa: E402
from src import bar_archive  # noqa: E402

# the exact params of candidate #7382 (params_json of strategy_candidates.id)
CFG = {"lookback": 12, "min_sweep_atr": 0.3, "disp_k": 1.0, "rr": 1.5,
       "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
       "atr_period": 14}

CODE_ORIG = (ROOT / "research" / "cycle_20261025" / "strategies" /
             "market_entry_ict_v1.py").read_text()
CODE_GUARD = (ROOT / "research" / "cycle_20261027" / "strategies" /
              "market_entry_sweep_cont_atrguard_v1.py").read_text()

SPLIT = "2026-06-11"          # the #7382 IS/HOLD boundary


def load(symbol: str, tf: str) -> pd.DataFrame:
    df = bar_archive.read_archive(symbol, tf).copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def real_only(df: pd.DataFrame) -> pd.DataFrame:
    keep = ((df["high"] - df["low"]) > 0) & (df["volume"] > 0)
    return df[keep].reset_index(drop=True)


def run(code, bars, params, symbol):
    b, lv, fa, meta = run_books(code, bars, params, symbol)
    return metrics(lv), metrics(b), meta


def main() -> int:
    out = {}
    for symbol, tf in (("MGC", "15m"),):
        df = load(symbol, tf)
        hold = df[df["ts"] < SPLIT].reset_index(drop=True)
        isw = df[df["ts"] >= SPLIT].reset_index(drop=True)
        hold_real = real_only(hold)

        # how dirty is the holdout?
        zr = int(((hold["high"] - hold["low"]) <= 0).sum())
        zv = int((hold["volume"] <= 0).sum())
        print(f"=== {symbol} {tf}  HOLD {hold['ts'].iloc[0]} -> "
              f"{hold['ts'].iloc[-1]}  n_bars={len(hold)}")
        print(f"    no-trade bars: range==0 {zr} ({100.0*zr/len(hold):.1f}%), "
              f"volume==0 {zv} ({100.0*zv/len(hold):.1f}%)")
        print(f"    real bars kept by the range+volume filter: {len(hold_real)}")

        rows = []
        # A: exactly what #7382 claims -- all bars, ATR from all bars
        m, mb, meta = run(CODE_ORIG, hold, dict(CFG, setup="sweep_cont"), symbol)
        rows.append(("A all-bars ATR(all)", m, mb, meta))
        # B: all bars (continuity preserved), ATR from REAL bars only
        m2, mb2, meta2 = run(CODE_GUARD, hold,
                             dict(CFG, atr_real_only=True), symbol)
        rows.append(("B all-bars ATR(real only)", m2, mb2, meta2))
        # C: real bars only (no-trade bars dropped) -- continuity broken
        m3, mb3, meta3 = run(CODE_ORIG, hold_real,
                             dict(CFG, setup="sweep_cont"), symbol)
        rows.append(("C real-bars only", m3, mb3, meta3))

        print(f"\n  {'book':<28} {'n':>5} {'PF':>7} {'E[R]':>7} "
              f"{'WR%':>6} {'usd':>10} {'maxDD':>10} {'sig':>5}")
        for name, m, mb, meta in rows:
            print(f"  {name:<28} {m['n']:>5} {m['pf']:>7.3f} {m['e_r']:>+7.3f} "
                  f"{m['wr_pct']:>6.1f} {m['total_usd']:>10.2f} "
                  f"{m['max_dd']:>10.2f} {meta.get('n_signals', 0):>5}")

        # D: the IS window under the same three books
        print(f"\n=== {symbol} {tf}  IS {isw['ts'].iloc[0]} -> "
              f"{isw['ts'].iloc[-1]}  n_bars={len(isw)}")
        rows_is = []
        m, mb, meta = run(CODE_ORIG, isw, dict(CFG, setup="sweep_cont"), symbol)
        rows_is.append(("A all-bars ATR(all)", m, mb, meta))
        m2, mb2, meta2 = run(CODE_GUARD, isw, dict(CFG, atr_real_only=True),
                             symbol)
        rows_is.append(("B all-bars ATR(real only)", m2, mb2, meta2))
        m3, mb3, meta3 = run(CODE_ORIG, real_only(isw),
                             dict(CFG, setup="sweep_cont"), symbol)
        rows_is.append(("C real-bars only", m3, mb3, meta3))
        print(f"  {'book':<28} {'n':>5} {'PF':>7} {'E[R]':>7} "
              f"{'WR%':>6} {'usd':>10} {'maxDD':>10} {'sig':>5}")
        for name, m, mb, meta in rows_is:
            print(f"  {name:<28} {m['n']:>5} {m['pf']:>7.3f} {m['e_r']:>+7.3f} "
                  f"{m['wr_pct']:>6.1f} {m['total_usd']:>10.2f} "
                  f"{m['max_dd']:>10.2f} {meta.get('n_signals', 0):>5}")

        out = {"symbol": symbol, "tf": tf, "split": SPLIT,
               "hold_bars": int(len(hold)),
               "hold_no_trade_range_pct": round(100.0 * zr / len(hold), 1),
               "hold_no_trade_vol_pct": round(100.0 * zv / len(hold), 1),
               "hold_real_bars": int(len(hold_real)),
               "hold": [{"book": n, "live": m, "base": mb,
                         "n_signals": meta.get("n_signals", 0)}
                        for n, m, mb, meta in rows],
               "is": [{"book": n, "live": m, "base": mb,
                       "n_signals": meta.get("n_signals", 0)}
                      for n, m, mb, meta in rows_is]}

    p = ROOT / "outputs" / "atr_quality_audit_20261027.json"
    p.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
