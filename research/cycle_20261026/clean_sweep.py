"""CLEAN-WINDOW multi-symbol market-entry sweep with a true holdout (2026-10-26).

WHY THIS FILE EXISTS
--------------------
`research/cycle_20261026/quality_scan.py` shows that the "deep" intraday
archive is mostly PLACEHOLDER data: for MGC 15m only 2026-07-21 -> present
is a contiguous run of bars with a real range AND real volume; the 2025-11 ->
2026-07 tail is 45-100% zero-range with 3-74% volume coverage.  The previous
cycle's "TRUE OOS holdout" (outputs/oos_holdout_MGC_15m_2026-06-11.json) put
its HOLD window squarely inside that degraded tail -- so that holdout number,
and the OOS claim attached to pending candidate #7382, are CONTAMINATED.

This harness therefore:
  1. keeps only bars with (high > low) and (volume > 0);
  2. keeps only the LONGEST contiguous clean run per symbol/timeframe;
  3. grid-searches on the FIRST 60% of that run (IS);
  4. re-runs the top configs on the LAST 40% (OOS, never seen by the grid);
  5. scores every book with the LIVE fill (market at the next bar's OPEN) --
     the production fill, per findings #9134/#9146.

Usage:
  .venv/bin/python research/cycle_20261026/clean_sweep.py <SYMBOL> <tf> [stage]
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import metrics, run_books  # noqa: E402
from src import bar_archive  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261025" / "strategies" /
        "market_entry_ict_v1.py").read_text()

IS_FRAC = 0.60
MIN_RUN_BARS = 1200


# ------------------------------------------------------------------ data ---
def clean_run(symbol: str, tf: str) -> pd.DataFrame | None:
    """Longest contiguous run of bars with a real range and real volume."""
    df = bar_archive.read_archive(str(symbol).upper(), tf)
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    clean = ((df["high"] - df["low"]) > 0) & (df["volume"] > 0)
    grp = (clean != clean.shift()).cumsum()
    best = None
    for _, idx in df.groupby(grp).groups.items():
        if len(idx) < MIN_RUN_BARS:
            continue
        seg = df.loc[idx].reset_index(drop=True)
        if best is None or len(seg) > len(best):
            best = seg
    return best


def split_run(run: pd.DataFrame):
    cut = int(len(run) * IS_FRAC)
    return run.iloc[:cut].reset_index(drop=True), run.iloc[cut:].reset_index(drop=True)


# ------------------------------------------------------------------ grid ---
def grid_cells():
    cells = []
    # --- sweep_cont: sweep -> displacement continuation (the MGC winner) ---
    for lb, ms, dk, rr, br in itertools.product(
            (8, 12, 20, 32), (0.15, 0.30, 0.45), (0.8, 1.0, 1.2),
            (1.0, 1.5, 2.0), (0.55, 0.70)):
        cells.append({"setup": "sweep_cont", "lookback": lb,
                      "min_sweep_atr": ms, "disp_k": dk, "rr": rr,
                      "confirm_bars": 6, "body_ratio_min": br,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    # --- sweep_rev: fade the failed breakout --------------------------------
    for lb, ms, rr in itertools.product((8, 12, 20, 32),
                                        (0.15, 0.30, 0.45), (1.0, 1.5, 2.0)):
        cells.append({"setup": "sweep_rev", "lookback": lb,
                      "min_sweep_atr": ms, "rr": rr,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    # --- disp_break: displacement closing beyond a rolling level ------------
    for lb, dk, rr, sa in itertools.product((8, 12, 20, 32), (0.8, 1.0, 1.2),
                                            (1.0, 1.5, 2.0), (0.6, 0.8, 1.2)):
        cells.append({"setup": "disp_break", "lookback": lb, "disp_k": dk,
                      "rr": rr, "stop_atr": sa, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    # --- disp_pullback: displacement -> shallow pullback holds --------------
    for lb, pb, rc, rr in itertools.product((12, 20), (3, 5, 8),
                                             (0.3, 0.5, 0.7), (1.0, 1.5, 2.0)):
        cells.append({"setup": "disp_pullback", "lookback": lb,
                      "pullback_bars": pb, "retrace": rc, "rr": rr,
                      "disp_k": 1.0, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    return cells


def _job(args):
    code, symbol, params, seg_bars = args
    b, lv, fa, meta = run_books(code, seg_bars, params, symbol)
    return {"params": params, "live": metrics(lv), "base": metrics(b),
            "fair": metrics(fa), "meta": meta}


def sweep(symbol: str, tf: str, cells, seg_bars, workers=12):
    jobs = [(CODE, symbol, c, seg_bars) for c in cells]
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_job, jobs, chunksize=2):
            rows.append(r)
    return rows


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    stage = sys.argv[3] if len(sys.argv) > 3 else "all"

    run = clean_run(symbol, tf)
    if run is None:
        print(f"{symbol} {tf}: no clean run >= {MIN_RUN_BARS} bars")
        return 1
    is_bars, oos_bars = split_run(run)
    print(f"{symbol} {tf}: clean run {len(run)} bars "
          f"{run['ts'].iloc[0]} -> {run['ts'].iloc[-1]}")
    print(f"  IS  {len(is_bars)} bars {is_bars['ts'].iloc[0]} -> {is_bars['ts'].iloc[-1]}")
    print(f"  OOS {len(oos_bars)} bars {oos_bars['ts'].iloc[0]} -> {oos_bars['ts'].iloc[-1]}")

    cells = grid_cells()
    print(f"  grid: {len(cells)} cells")

    is_rows = sweep(symbol, tf, cells, is_bars)
    # rank by LIVE PF, require a real sample on IS
    is_rows.sort(key=lambda r: -(r["live"]["pf"] if r["live"]["n"] >= 20 else -1))
    print("\n=== TOP 12 IS cells (LIVE book) ===")
    for r in is_rows[:12]:
        print(f"  {json.dumps(r['params'])}")
        print(f"      IS live {json.dumps(r['live'])}")

    top = is_rows[:20]
    oos_rows = sweep(symbol, tf, [r["params"] for r in top], oos_bars)
    oos_by = {json.dumps(r["params"], sort_keys=True): r for r in oos_rows}

    print(f"\n=== IS vs OOS (LIVE book) ===")
    print(f"{'params':<96} {'IS n':>5} {'IS pf':>6} {'OOS n':>6} {'OOS pf':>7} "
          f"{'OOS $':>10}  verdict")
    survivors = []
    for r in top:
        k = json.dumps(r["params"], sort_keys=True)
        o = oos_by.get(k)
        if not o:
            continue
        il, ol = r["live"], o["live"]
        ok = (il["pf"] > 1.0 and ol["pf"] > 1.0 and ol["n"] >= 15
              and il["n"] >= 20)
        tag = json.dumps({k2: v for k2, v in r["params"].items()
                          if k2 not in ("atr_period", "confirm_bars")})
        if ok:
            survivors.append({"params": r["params"], "is": il, "oos": ol,
                              "is_base": r["base"], "oos_base": o["base"]})
        print(f"{tag:<96} {il['n']:>5} {il['pf']:>6.3f} {ol['n']:>6} "
              f"{ol['pf']:>7.3f} {ol['total_usd']:>10.2f}  "
              f"{'SURVIVOR' if ok else ''}")

    print(f"\n=== {len(survivors)} survivors (IS pf>1 & OOS pf>1, IS n>=20, OOS n>=15) ===")
    for s in survivors:
        print(f"  {json.dumps(s['params'])}")
        print(f"      IS  live {json.dumps(s['is'])}")
        print(f"      OOS live {json.dumps(s['oos'])}")

    p = ROOT / "outputs" / f"clean_sweep_{symbol}_{tf}_20261026.json"
    p.write_text(json.dumps(
        {"symbol": symbol, "tf": tf,
         "clean_run": [str(run["ts"].iloc[0]), str(run["ts"].iloc[-1])],
         "n_clean_bars": int(len(run)),
         "is": [str(is_bars["ts"].iloc[0]), str(is_bars["ts"].iloc[-1])],
         "oos": [str(oos_bars["ts"].iloc[0]), str(oos_bars["ts"].iloc[-1])],
         "n_cells": len(cells), "survivors": survivors}, indent=1, default=str))
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
