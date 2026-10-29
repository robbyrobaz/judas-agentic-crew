"""Independent verification of candidate #7383 (MGC 5m sweep_cont hygiene).

Two independent measurements:

  (A) FAIR / PRODUCTION FILL  (research honest_screen 'live' book):
      market at the OPEN of the bar AFTER the decision bar, brackets
      re-anchored, gap-aware exits, cost_model=v1_realistic_micros, qty=1.
      Data: per CLEAN SEGMENT (placeholder rows dropped).
      IS/OOS: time split, first 60% IS, last 40% OOS.

  (B) HOUSE ENGINE (src/research/custom_strategy_runtime.run_custom_backtest_on_bars):
      the path whose retro-fill defect is documented in finding 07f3a4c2 /
      9126.  Run here ONLY to read the stale-fill counters (stale_fill_n,
      stale_measure_fail_n) and confirm csid441 is NOT in the stale class.

Run: .venv/bin/python research/cycle_20261029/verify_cand7383.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

import pandas as pd  # noqa: E402

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments  # noqa: E402

CELL = {
    "lookback": 20, "min_sweep_atr": 0.6, "disp_k": 1.0, "rr": 2.0,
    "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
    "atr_period": 14,
}
SYMBOL, TF = "MGC", "5m"
MIN_SEG = 300
IS_FRAC = 0.60


def load_code(csid: int) -> str:
    conn = sqlite3.connect(ROOT / "judas_crew.db")
    try:
        row = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=? AND active=1", (csid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit(f"csid {csid} not found/active")
    return str(row[0])


def main() -> None:
    code = load_code(441)
    segs = clean_segments(SYMBOL, TF, MIN_SEG)
    if not segs:
        raise SystemExit("no clean segments")
    t0 = min(s["ts"].iloc[0] for s in segs)
    t1 = max(s["ts"].iloc[-1] for s in segs)
    split = t0 + (t1 - t0) * IS_FRAC
    print(f"{SYMBOL} {TF}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, {t0} -> {t1}")
    print(f"IS/OOS split at {split}", flush=True)

    live, base = [], []
    for seg in segs:
        b_t, l_t, meta = run_books(code, seg, CELL, SYMBOL, budget_s=900.0)
        if meta.get("load_error"):
            raise SystemExit(f"load error: {meta}")
        if meta.get("timed_out"):
            print("  WARNING: segment timed out", flush=True)
        seg_ts = seg["ts"].reset_index(drop=True)
        for book in (b_t, l_t):
            for t in book:
                try:
                    t["entry_ts"] = seg_ts.iloc[int(t["entry_bar"])]
                except Exception:  # noqa: BLE001
                    t["entry_ts"] = t0
        base += b_t
        live += l_t

    out = {}
    for name, book in (("live_production_fill", live), ("house_named_price_fill", base)):
        is_t = [t for t in book if t["entry_ts"] < split]
        oos_t = [t for t in book if t["entry_ts"] >= split]
        out[name] = {"is": metrics(is_t), "oos": metrics(oos_t), "all": metrics(book)}
    print(json.dumps(out, indent=2, default=str))

    # ---- house engine: stale-fill counters only -------------------------
    from src.research.custom_strategy_runtime import run_custom_backtest_on_bars
    agg = {"stale_fill_n": 0, "stale_measure_fail_n": 0, "n_signals": 0,
           "n_wins": 0, "n_losses": 0, "pf": 0.0, "total_pnl": 0.0,
           "expectancy_r": 0.0, "max_drawdown": 0.0}
    for seg in segs:
        r = run_custom_backtest_on_bars(
            code=code, bars=seg, params=CELL, symbol=SYMBOL, qty=1, timeout_s=900
        )
        for k in agg:
            agg[k] += r.get(k, 0) or 0
    print("\nHOUSE ENGINE aggregate over clean segments:")
    print(json.dumps(agg, indent=2, default=str))


if __name__ == "__main__":
    main()
