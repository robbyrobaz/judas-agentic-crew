"""HONEST LIVE-BOOK SCREEN -- sweep-continuation with selectable POOL
(rolling extreme | equal-highs/lows cluster) and the 50%-MITIGATION
(inducement) filter.  2026-10-28c.

Same machinery as research/cycle_20261027b/honest_live_screen.py:

  * CLEAN SEGMENTS only (placeholder / dirty bars dropped),
  * THREE books from the SAME signals:
      base  = fill at the named price (what the house backtester stamps),
      fair  = fill at the decision bar's close,
      live  = MARKET at the next bar's open, brackets re-anchored,
              gap-aware exits  <-- the production fill, the number we rank on,
  * IS/OOS split BY TIME over the global clean span (first 60% IS), so the
    OOS window is never seen by the grid,
  * a GEOMETRY GUARD on every signal (finding 9145).

usage: .venv/bin/python research/cycle_20261028c/screen_unified.py <SYMBOL> <tf>
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

CODE = (ROOT / "research" / "cycle_20261028c" / "strategies" /
        "sweep_pool_mit50_v1.py").read_text()

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15


def grid_cells():
    cells = []
    for pool, mit, ms, dk, rr, win in itertools.product(
            ("rolling", "equal"), (0, 1), (0.45, 0.60), (0.8, 1.0),
            (1.5, 2.0), (0, 1)):
        c = {"setup": "sweep_cont", "pool_mode": pool, "mit50": mit,
             "min_sweep_atr": ms, "disp_k": dk, "rr": rr,
             "disp_mode": "through", "body_ratio_min": 0.55,
             "stop_buf_atr": 0.15, "atr_period": 14, "confirm_bars": 6,
             "hist_bars": 320}
        if pool == "rolling":
            c["lookback"] = (8, 20)[win]
            c["cluster_age"] = 40
        else:
            c["lookback"] = 20
            c["cluster_age"] = (40, 60)[win]
            c["pivot_len"] = 2
            c["cluster_atr"] = 0.15
            c["min_pivots"] = 2
        cells.append(c)
    return cells


def _job(args):
    symbol, tf, params = args
    segs = clean_segments(symbol, tf)
    if not segs:
        return None
    boundary = split_ts(symbol, tf, segs)
    per = {"base": [], "live": [], "fair": []}
    for seg in segs:
        b, lv, fa, meta = run_books(CODE, seg, params, symbol)
        if meta.get("load_error"):
            return None
        per["base"].append(b)
        per["live"].append(lv)
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


def _tag(p):
    return json.dumps({k: v for k, v in p.items()
                       if k not in ("atr_period", "confirm_bars", "setup",
                                    "disp_mode", "hist_bars")}, sort_keys=True)


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: no clean segment")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}", flush=True)

    cells = grid_cells()
    print(f"  grid: {len(cells)} cells", flush=True)

    rows = []
    workers = int(os.environ.get("SEG_WORKERS", "8"))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_job, [(symbol, tf, c) for c in cells], chunksize=2):
            if r:
                rows.append(r)

    rows.sort(key=lambda r: -(r["live"]["is"]["pf"]
                              if r["live"]["is"]["n"] >= MIN_TRADES_IS else -1))

    survivors = []
    for r in rows:
        il, ol = r["live"]["is"], r["live"]["oos"]
        if (il["pf"] > 1.0 and ol["pf"] > 1.0 and il["n"] >= MIN_TRADES_IS
                and ol["n"] >= MIN_TRADES_OOS):
            survivors.append(r)

    print("\n=== TOP 12 IS cells (LIVE book) ===")
    for r in rows[:12]:
        print(f"  {_tag(r['params'])}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      base IS {r['base']['is']['pf']}  fair IS {r['fair']['is']['pf']}")

    print(f"\n=== {len(survivors)} survivors "
          f"(IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, OOS n>={MIN_TRADES_OOS}) ===")
    for r in sorted(survivors, key=lambda r: -(r["live"]["oos"]["pf"])):
        print(f"  {_tag(r['params'])}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      full {json.dumps(r['live']['full'])}")
        print(f"      base full {r['base']['full']['pf']}  fair full {r['fair']['full']['pf']}")

    out = ROOT / "outputs" / f"unified_{symbol}_{tf}_20261028c.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)),
         "boundary": str(boundary), "n_cells": len(cells),
         "survivors": survivors, "top_is": rows[:80]}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
