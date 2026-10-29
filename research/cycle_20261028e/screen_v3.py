"""HONEST LIVE-BOOK SCREEN -- sweep-continuation v3, new axes (2026-10-28e).

Same machinery as research/cycle_20261028d/screen_filters.py:
  * CLEAN SEGMENTS only (placeholder / dirty bars dropped),
  * THREE books from the SAME signals:
      base = fill at the named price (what the house backtester stamps),
      fair = fill at the decision bar's close,
      live = MARKET at the next bar's open, brackets re-anchored,
             gap-aware exits  <-- the production fill, the number we rank on,
  * IS/OOS split BY TIME over the global clean span (first 60% IS),
  * a GEOMETRY GUARD on every signal.

NEW AXES screened here (research/cycle_20261028e/strategies/sweep_cont_v3.py):
  sess_mode, sweep_body_max, target_mode/tgt_window/min_rr.

usage: .venv/bin/python research/cycle_20261028e/screen_v3.py <SYMBOL> <tf> [grid]
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

CODE = (ROOT / "research" / "cycle_20261028e" / "strategies" /
        "sweep_cont_v3.py").read_text()

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15

BASE = {
    "setup": "sweep_cont",
    "pool_mode": "rolling",
    "lookback": 40,
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
    "sess_mode": 0,
    "sweep_body_max": 1.0,
    "target_mode": 0,
    "tgt_window": 60,
    "min_rr": 0.5,
}


def _c(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


def grid_axes():
    """One axis at a time around the MGC 5m lb40 base cell."""
    cells = [("base", _c())]
    for s in (1, 2, 3, 4, 5):
        cells.append((f"sess{s}", _c(sess_mode=s)))
    for b in (0.60, 0.40, 0.25):
        cells.append((f"sbody{int(b*100):03d}", _c(sweep_body_max=b)))
    for tw in (40, 80, 120):
        cells.append((f"pool_tw{tw}", _c(target_mode=1, tgt_window=tw)))
        cells.append((f"pool_tw{tw}_mr10", _c(target_mode=1, tgt_window=tw, min_rr=1.0)))
    cells.append(("trend", _c(trend_mode=1)))
    cells.append(("lb20", _c(lookback=20)))
    cells.append(("lb60", _c(lookback=60)))
    return cells


def grid_combo():
    """Combinations of the axes that survived the one-at-a-time pass."""
    cells = [("base", _c())]
    for s in (1, 2, 3):
        for b in (0.60, 0.40):
            cells.append((f"sess{s}_sb{int(b*100):03d}", _c(sess_mode=s, sweep_body_max=b)))
    for s in (1, 2, 3):
        for tw in (40, 80):
            cells.append((f"sess{s}_pool{tw}", _c(sess_mode=s, target_mode=1, tgt_window=tw)))
    for b in (0.60, 0.40):
        for tw in (40, 80):
            cells.append((f"sb{int(b*100):03d}_pool{tw}", _c(sweep_body_max=b, target_mode=1, tgt_window=tw)))
    return cells


def grid_core():
    """Small transfer grid used for the cross-symbol sweep (6 cells)."""
    cells = [
        ("base_lb40", _c()),
        ("lb20", _c(lookback=20)),
        ("sbody060", _c(sweep_body_max=0.60)),
        ("cb12", _c(confirm_bars=12)),
        ("pool_tw40", _c(target_mode=1, tgt_window=40)),
        ("vol15", _c(vol_k=1.5)),
    ]
    return cells


GRIDS = {"axes": grid_axes, "combo": grid_combo, "core": grid_core}


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
    grid_name = sys.argv[3] if len(sys.argv) > 3 else "axes"
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: no clean segment")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}", flush=True)

    cells = GRIDS[grid_name]()
    print(f"  grid {grid_name}: {len(cells)} cells", flush=True)

    rows = []
    workers = int(os.environ.get("SEG_WORKERS", "6"))
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_job, [(tag, symbol, tf, p) for tag, p in cells],
                        chunksize=1):
            if r:
                rows.append(r)
                print(f"  done {r['tag']} ({time.time() - t0:.0f}s)", flush=True)

    rows.sort(key=lambda r: -(r["live"]["is"]["pf"] if r["live"]["is"]["n"] >= MIN_TRADES_IS else -1))

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
              f"base_full={r['base']['full']['pf']:.3f} "
              f"fair_full={r['fair']['full']['pf']:.3f}")

    print(f"\n=== {len(survivors)} survivors "
          f"(IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, OOS n>={MIN_TRADES_OOS}) ===")
    for r in sorted(survivors, key=lambda r: -(r["live"]["oos"]["pf"])):
        print(f"  {r['tag']:22s} IS {json.dumps(r['live']['is'])}")
        print(f"  {'':22s} OOS {json.dumps(r['live']['oos'])}")
        print(f"  {'':22s} base full {json.dumps(r['base']['full'])}")

    out = ROOT / "outputs" / f"v3_{grid_name}_{symbol}_{tf}_20261028e.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "grid": grid_name,
         "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)),
         "boundary": str(boundary), "n_cells": len(cells),
         "survivors": survivors, "all": rows}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
