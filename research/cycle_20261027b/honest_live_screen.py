"""HONEST LIVE-BOOK SCREEN for cycle 20261027b (market-entry sweep family).

Same machinery as research/cycle_20261026/seg_sweep.py and
research/cycle_20261025/honest_screen.py:

  * CLEAN SEGMENTS only (placeholder/dirty bars dropped),
  * THREE books from the SAME signals: base (fill at the named price = what
    the house backtester stamps), fair (fill at the decision close) and live
    (MARKET at the next bar's open, brackets re-anchored, gap-aware exits --
    the production fill),
  * IS/OOS split BY TIME over the global clean span (first 60% IS) so the OOS
    window is never seen by the grid,
  * a GEOMETRY GUARD on every signal (stop on the losing side of the entry,
    target on the winning side) -- finding 9145.

Usage:
  .venv/bin/python research/cycle_20261027b/honest_live_screen.py <SYMBOL> <tf>
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

CODE = (ROOT / "research" / "cycle_20261027b" / "strategies" /
        "sweep_cont_market_entry_v2.py").read_text()

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15


def grid_cells():
    cells = []
    # --- sweep_cont: rolling-extreme sweep -> displacement -> continuation ---
    for lb, ms, dk, rr, mode in itertools.product(
            (12, 20, 32), (0.15, 0.30), (0.8, 1.0), (1.0, 1.5, 2.0),
            ("through", "beyond")):
        cells.append({"setup": "sweep_cont", "lookback": lb,
                      "min_sweep_atr": ms, "disp_k": dk, "rr": rr,
                      "disp_mode": mode, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14,
                      "confirm_bars": 6, "ema_period": 0,
                      "session_start_utc": -1, "session_end_utc": -1})
    # --- pd_sweep_cont: previous-DAY high/low sweep -> continuation ---------
    for ms, dk, rr, mode in itertools.product(
            (0.15, 0.30), (0.8, 1.0), (1.0, 1.5, 2.0), ("through", "beyond")):
        cells.append({"setup": "pd_sweep_cont", "lookback": 12,
                      "min_sweep_atr": ms, "disp_k": dk, "rr": rr,
                      "disp_mode": mode, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14,
                      "confirm_bars": 6, "ema_period": 0,
                      "session_start_utc": -1, "session_end_utc": -1,
                      "pd_max_age_days": 4})
    # --- sess_sweep: session-pool sweep (Asia/London/NY) in the kill zones --
    for ms, dk, rr, smax, conf in itertools.product(
            (0.15, 0.30), (0.8, 1.0), (1.0, 1.5, 2.0), (2.5, 99.0), (4, 8)):
        cells.append({"setup": "sess_sweep", "lookback": 12,
                      "min_sweep_atr": ms, "disp_k": dk, "rr": rr,
                      "disp_mode": "through", "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14,
                      "confirm_bars": conf, "ema_period": 0,
                      "session_start_utc": -1, "session_end_utc": -1,
                      "sweep_max_atr": smax})
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
                       if k not in ("atr_period", "confirm_bars",
                                    "session_start_utc", "session_end_utc",
                                    "ema_period", "pd_max_age_days")},
                      sort_keys=True)


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
    workers = int(os.environ.get("SEG_WORKERS", "10"))
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

    print(f"\n=== TOP 12 IS cells (LIVE book) ===")
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

    out = ROOT / "outputs" / f"hlive_{symbol}_{tf}_20261027b.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)),
         "boundary": str(boundary), "n_cells": len(cells),
         "survivors": survivors, "top_is": rows[:60]}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
