"""IFVG (Zen sweep model) honest screen -- 2026-10-28 cycle.

Same machinery as research/cycle_20261027/liq_screen.py:
  * clean segments (placeholder/dirty bars dropped),
  * LIVE fill = market at the NEXT bar's open, brackets re-anchored
    (production semantics -- the live path submits a MARKET order),
  * base / fair / live books computed from the SAME signals, so a
    retro-fill premium shows up as base != live,
  * IS/OOS split by TIME over the global clean span (first 60% IS).

PLUS a BETA CHECK (drift attribution, finding 9153): every trade is bucketed
by the direction the MARKET moved while it was open.  A long-only system that
only wins in rising buckets is beta, not edge, and must not be proposed.

Usage:
  .venv/bin/python research/cycle_20261028/ifvg_screen.py <SYMBOL> <tf>
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
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261028" / "strategies" /
        "market_entry_sweep_ifvg_v1.py").read_text()

IS_FRAC = 0.60
MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15


def grid_cells():
    cells = []
    for lb, ms, fg, edge, sp, rr in itertools.product(
            (8, 20), (0.15, 0.30, 0.45), (0.10, 0.20), ("near", "far"),
            (0, 2), (1.0, 1.5, 2.0)):
        cells.append({"lookback": lb, "min_sweep_atr": ms,
                      "fvg_atr_min": fg, "fvg_max_age": 12,
                      "ifvg_edge": edge, "struct_min_pools": sp,
                      "struct_bars": 24, "rr": rr, "confirm_bars": 6,
                      "disp_k": 0.8, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    return cells


def _market_bucket(seg, trade):
    """Sign of the market move over the trade's holding window."""
    c = seg["close"].to_numpy(float)
    eb = int(trade["entry_bar"])
    xb = int(trade["exit_bar"])
    if eb <= 0 or xb >= len(c):
        return 0
    if c[xb] > c[eb - 1] * 1.0002:
        return 1
    if c[xb] < c[eb - 1] * 0.9998:
        return -1
    return 0


def _beta_table(segs, trades):
    """PF of the trades split by (trade direction, market direction)."""
    buckets: dict[str, list] = {}
    for seg, t in zip(segs, trades):
        mb = _market_bucket(seg, t)
        key = f"{t['direction']}/mkt{'up' if mb > 0 else ('dn' if mb < 0 else 'flat')}"
        buckets.setdefault(key, []).append(t)
    return {k: {"n": metrics(v)["n"], "pf": metrics(v)["pf"],
                "usd": metrics(v)["total_usd"]}
            for k, v in sorted(buckets.items())}


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
    res["beta"] = _beta_table(segs, per["live"])
    return res


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: no clean segment >= 300 bars")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}")

    cells = grid_cells()
    print(f"  grid: {len(cells)} cells")
    rows = []
    with ProcessPoolExecutor(max_workers=int(os.environ.get("SEG_WORKERS", "6"))) as ex:
        for r in ex.map(_job, [(symbol, tf, c) for c in cells], chunksize=2):
            if r:
                rows.append(r)

    rows.sort(key=lambda r: -(r["live"]["is"]["pf"]
                              if r["live"]["is"]["n"] >= MIN_TRADES_IS else -1))
    print("\n=== TOP 12 IS cells (LIVE book) ===")
    for r in rows[:12]:
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "confirm_bars")})
        print(f"  {tag}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      base full {r['base']['full']['pf']} fair full "
              f"{r['fair']['full']['pf']} (agree => honest fill)")

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
                          if k not in ("atr_period", "confirm_bars")})
        print(f"  {tag}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      full {json.dumps(r['live']['full'])}")
        print(f"      beta {json.dumps(r['beta'])}")

    out = ROOT / "outputs" / f"ifvg_screen_{symbol}_{tf}_20261028.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)), "boundary": str(boundary),
         "n_cells": len(cells), "survivors": survivors,
         "top_is": rows[:40]}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
