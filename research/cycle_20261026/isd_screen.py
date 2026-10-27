"""ISD (inducement -> sweep -> displacement) SEGMENT SCREEN — 2026-10-26 cycle.

Same honest machinery as seg_sweep.py (clean segments, LIVE fill = market at
the next bar's open, IS/OOS split by time on the global clean span), but the
strategy under test is the NEW ISD family extracted from YT:jmlDR7nop-8
(finding 79b56fae):

    inducement (pivot level) -> sweep -> displacement -> retrace into the FVG
    entry = the decision bar's CLOSE (never the FVG midpoint: retro-fill)

Usage:
  .venv/bin/python research/cycle_20261026/isd_screen.py <SYMBOL> <tf>
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261026" / "strategies" /
        "market_entry_isd_v1.py").read_text()

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15


def grid_cells():
    cells = []
    for setup, pp, ib, ms, dk, rr, br in itertools.product(
            ("isd", "isd_nofvg"), (2, 3, 5), (40, 80), (0.15, 0.30, 0.45),
            (0.8, 1.0, 1.2), (1.0, 1.5, 2.0), (0.55, 0.70)):
        cells.append({"setup": setup, "pivot_p": pp, "ind_bars": ib,
                      "pullback_atr": 0.25, "min_sweep_atr": ms,
                      "disp_k": dk, "body_ratio_min": br, "retrace": 0.5,
                      "rr": rr, "stop_buf_atr": 0.15, "atr_period": 14})
    return cells


def _job(args):
    symbol, tf, params = args
    segs = clean_segments(symbol, tf)
    if not segs:
        return None
    boundary = split_ts(symbol, tf, segs)
    per = {"live": [], "base": [], "fair": []}
    for seg in segs:
        b, lv, fa, meta = run_books(CODE, seg, params, symbol)
        if meta.get("load_error"):
            return None
        per["live"].append(lv)
        per["base"].append(b)
        per["fair"].append(fa)

    def partition(kind):
        is_t, oos_t = [], []
        for seg, trades in zip(segs, per[kind]):
            for t in trades:
                ts = seg["ts"].iloc[t["entry_bar"]]
                (is_t if ts <= boundary else oos_t).append(t)
        return is_t, oos_t

    res = {"params": params, "n_seg": len(segs),
           "n_bars": int(sum(len(s) for s in segs))}
    for kind in ("live", "base", "fair"):
        is_t, oos_t = partition(kind)
        res[kind] = {"is": metrics(is_t), "oos": metrics(oos_t),
                     "full": metrics(is_t + oos_t)}
    return res


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: no clean segment")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}")
    cells = grid_cells()
    print(f"  grid: {len(cells)} cells")

    rows = []
    with ProcessPoolExecutor(max_workers=12) as ex:
        for r in ex.map(_job, [(symbol, tf, c) for c in cells], chunksize=2):
            if r:
                rows.append(r)

    rows.sort(key=lambda r: -(r["live"]["is"]["pf"]
                              if r["live"]["is"]["n"] >= MIN_TRADES_IS else -1))
    print("\n=== TOP 12 IS cells (LIVE book) ===")
    for r in rows[:12]:
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "pullback_atr")})
        print(f"  {tag}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      base IS {r['base']['is']['pf']}  fair IS "
              f"{r['fair']['is']['pf']}  (agree => honest fill)")

    survivors = [r for r in rows
                 if r["live"]["is"]["pf"] > 1.0
                 and r["live"]["oos"]["pf"] > 1.0
                 and r["live"]["is"]["n"] >= MIN_TRADES_IS
                 and r["live"]["oos"]["n"] >= MIN_TRADES_OOS]
    print(f"\n=== {len(survivors)} survivors ===")
    for r in sorted(survivors, key=lambda r: -(r["live"]["oos"]["pf"])):
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "pullback_atr")})
        print(f"  {tag}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      full {json.dumps(r['live']['full'])}")

    out = ROOT / "outputs" / f"isd_screen_{symbol}_{tf}_20261026.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)),
         "boundary": str(boundary), "n_cells": len(cells),
         "survivors": survivors, "top_is": rows[:40]}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
