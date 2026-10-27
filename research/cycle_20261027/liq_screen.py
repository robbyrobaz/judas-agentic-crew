"""LIQUIDITY-TARGET market-entry screen (2026-10-27 cycle).

Same honest machinery as research/cycle_20261026/seg_sweep.py:
  * clean segments (placeholder/dirty bars dropped),
  * LIVE fill = market at the NEXT bar's open, brackets re-anchored
    (production semantics -- the live path submits a MARKET order),
  * base / fair / live books computed from the SAME signals, so a
    retro-fill premium shows up as base != live,
  * IS/OOS split by TIME over the global clean span (first 60% IS).

The strategy under test is the NEW liquidity-target family extracted from
YT:l27wF-EvDkw (finding be32c6a1): the target is the opposite-side pool of
liquidity, not a fixed R multiple.

Usage:
  .venv/bin/python research/cycle_20261027/liq_screen.py <SYMBOL> <tf>
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

CODE = (ROOT / "research" / "cycle_20261027" / "strategies" /
        "market_entry_liq_target_v1.py").read_text()

IS_FRAC = 0.60
MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15


def grid_cells():
    cells = []
    for lb, ms, dk, br, rr, ll in itertools.product(
            (12, 32), (0.15, 0.30, 0.45), (0.8, 1.0), (0.55, 0.70),
            (1.5, 2.0), (48, 96, 192)):
        cells.append({"setup": "sweep_liq", "lookback": lb,
                      "min_sweep_atr": ms, "disp_k": dk,
                      "body_ratio_min": br, "min_rr": rr,
                      "liq_lookback": ll, "confirm_bars": 6,
                      "stop_buf_atr": 0.15, "atr_period": 14,
                      "ema_period": 0})
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


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: no clean segment")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}")

    cells = grid_cells()
    print(f"  grid: {len(cells)} cells")

    rows = []
    workers = int(os.environ.get("SEG_WORKERS", "5"))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_job, [(symbol, tf, c) for c in cells], chunksize=2):
            if r:
                rows.append(r)

    rows.sort(key=lambda r: -(r["live"]["is"]["pf"]
                              if r["live"]["is"]["n"] >= MIN_TRADES_IS else -1))
    print("\n=== TOP 12 IS cells (LIVE book) ===")
    for r in rows[:12]:
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "confirm_bars", "setup")})
        print(f"  {tag}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      base IS {r['base']['is']['pf']}  fair IS "
              f"{r['fair']['is']['pf']}  (agree => honest fill)")

    survivors = []
    for r in rows:
        il, ol = r["live"]["is"], r["live"]["oos"]
        if (il["pf"] > 1.0 and ol["pf"] > 1.0 and il["n"] >= MIN_TRADES_IS
                and ol["n"] >= MIN_TRADES_OOS):
            survivors.append(r)

    print(f"\n=== {len(survivors)} survivors "
          f"(IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, OOS n>={MIN_TRADES_OOS}) ===")
    for r in sorted(survivors, key=lambda r: -(r["live"]["oos"]["pf"])):
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "confirm_bars", "setup")})
        print(f"  {tag}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      full {json.dumps(r['live']['full'])}")
        print(f"      base full {r['base']['full']['pf']}  fair full "
              f"{r['fair']['full']['pf']}")

    out = ROOT / "outputs" / f"liq_screen_{symbol}_{tf}_20261027.json"
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
