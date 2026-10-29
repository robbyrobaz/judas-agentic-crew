"""HONEST LIVE-BOOK SCREEN -- sweep-continuation + NEW FILTERS (2026-10-28d).

Same machinery as research/cycle_20261028c/screen_unified.py:
  * CLEAN SEGMENTS only (placeholder / dirty bars dropped),
  * THREE books from the SAME signals:
      base = fill at the named price (what the house backtester stamps),
      fair = fill at the decision bar's close,
      live = MARKET at the next bar's open, brackets re-anchored,
             gap-aware exits  <-- the production fill, the number we rank on,
  * IS/OOS split BY TIME over the global clean span (first 60% IS),
  * a GEOMETRY GUARD on every signal.

Cells are the NEW-FILTER increments of this morning's ingest:
  trend_mode (structure trend: HH-HL / LH-LL),
  ext_window (external-only liquidity: no internal pullback pools),
  prom_atr  (pool must be a high-prominence swing pivot),
  mit50     (structural inducement filter),
  pool_mode "equal" (EQH/EQL cluster pool).

usage: .venv/bin/python research/cycle_20261028d/screen_filters.py <SYMBOL> <tf>
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261028d" / "strategies" /
        "sweep_pool_filters_v2.py").read_text()

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15

BASE = {
    "setup": "sweep_cont",
    "pool_mode": "rolling",
    "lookback": 20,
    "ext_window": 0,
    "prom_atr": 0.0,
    "prom_win": 20,
    "trend_mode": 0,
    "pivot_len": 2,
    "mit50": 0,
    "cluster_atr": 0.15,
    "min_pivots": 2,
    "cluster_age": 40,
    "min_sweep_atr": 0.60,
    "disp_k": 1.0,
    "disp_mode": "through",
    "body_ratio_min": 0.55,
    "rr": 2.0,
    "confirm_bars": 6,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "hist_bars": 320,
}


def _c(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


def grid_cells():
    cells = [
        ("base", _c()),
        ("trend", _c(trend_mode=1)),
        ("ext60", _c(ext_window=60)),
        ("ext120", _c(ext_window=120)),
        ("prom1", _c(prom_atr=1.0)),
        ("mit50", _c(mit50=1)),
        ("trend_ext60", _c(trend_mode=1, ext_window=60)),
        ("trend_prom1", _c(trend_mode=1, prom_atr=1.0)),
        ("trend_ext60_prom1", _c(trend_mode=1, ext_window=60, prom_atr=1.0)),
        ("trend_mit50", _c(trend_mode=1, mit50=1)),
        ("trend_rr15", _c(trend_mode=1, rr=1.5)),
        ("trend_ms045", _c(trend_mode=1, min_sweep_atr=0.45)),
        ("equal", _c(pool_mode="equal")),
        ("equal_trend", _c(pool_mode="equal", trend_mode=1)),
        ("equal_mit50", _c(pool_mode="equal", mit50=1)),
        ("equal_trend_ms045", _c(pool_mode="equal", trend_mode=1, min_sweep_atr=0.45)),
        ("lb12_ms030", _c(lookback=12, min_sweep_atr=0.30)),
        ("lb12_ms030_trend", _c(lookback=12, min_sweep_atr=0.30, trend_mode=1)),
        ("lb12_ms030_rr15", _c(lookback=12, min_sweep_atr=0.30, rr=1.5)),
        ("lb30_ms060", _c(lookback=30)),
        ("lb30_ms060_trend", _c(lookback=30, trend_mode=1)),
    ]
    return cells


def _job(args):
    tag, symbol, tf, params = args
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

    res = {"tag": tag, "params": params, "n_seg": len(segs),
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
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}", flush=True)

    cells = grid_cells()
    print(f"  grid: {len(cells)} cells", flush=True)

    rows = []
    workers = int(os.environ.get("SEG_WORKERS", "6"))
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_job, [(tag, symbol, tf, p) for tag, p in cells],
                        chunksize=1):
            if r:
                rows.append(r)
                print(f"  done {r['tag']} ({time.time() - t0:.0f}s)", flush=True)

    def sort_key(r):
        il = r["live"]["is"]
        return -(il["pf"] if il["n"] >= MIN_TRADES_IS else -1)

    rows.sort(key=sort_key)

    survivors = []
    for r in rows:
        il, ol = r["live"]["is"], r["live"]["oos"]
        if (il["pf"] > 1.0 and ol["pf"] > 1.0 and il["n"] >= MIN_TRADES_IS
                and ol["n"] >= MIN_TRADES_OOS):
            survivors.append(r)

    print("\n=== ALL CELLS (LIVE book) ===")
    for r in rows:
        il, ol, fl = r["live"]["is"], r["live"]["oos"], r["live"]["full"]
        print(f"  {r['tag']:22s} IS pf={il['pf']:.3f} n={il['n']:<4d} "
              f"OOS pf={ol['pf']:.3f} n={ol['n']:<4d} "
              f"FULL pf={fl['pf']:.3f} n={fl['n']:<4d} "
              f"base_full={r['base']['full']['pf']:.3f}")

    print(f"\n=== {len(survivors)} survivors "
          f"(IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, OOS n>={MIN_TRADES_OOS}) ===")
    for r in sorted(survivors, key=lambda r: -(r["live"]["oos"]["pf"])):
        print(f"  {r['tag']:22s} {json.dumps(r['live']['is'])}")
        print(f"  {'':22s} {json.dumps(r['live']['oos'])}")
        print(f"  {'':22s} base full {r['base']['full']}")

    out = ROOT / "outputs" / f"filters_{symbol}_{tf}_20261028d.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)),
         "boundary": str(boundary), "n_cells": len(cells),
         "survivors": survivors, "all": rows}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
