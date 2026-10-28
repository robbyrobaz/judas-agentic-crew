"""MARKET-ENTRY FAMILY PORT SCREEN (2026-10-28 cycle).

WHY
---
The only family in the book with a genuine, NON-DIRECTIONAL edge is the
market-entry sweep-continuation architecture (csid 439 / candidate 7382,
promoted on MGC 15m).  Drift attribution (finding 9153) proved it is not
beta: it is profitable in the buckets where the market FELL.  The same
finding killed the MNQ port (live PF 0.91) and left ZF and 6J UNTESTED.

Gate 4 of the promotion pipeline rejects any candidate whose stamped PF is a
retro-fill artefact, so every screen here is scored on the LIVE book
(fill at the OPEN of the bar after the decision bar, brackets re-anchored,
gap-aware exits, cost_model=v1_realistic_micros, qty=1).  The house engine
fills at the named price, which for a market-entry strategy is the decision
bar's close -- i.e. the honest price.

ANCHORED GRID
-------------
The grid is deliberately SMALL and anchored on the ONE cell that was
validated out-of-sample on MGC (csid 439: lookback 12, min_sweep_atr 0.30,
disp_k 1.0, rr 1.5, body_ratio_min 0.55, stop_buf_atr 0.15, confirm_bars 6,
disp_mode through).  A wide per-symbol search would fit noise; a port of one
validated cell is a falsifiable claim: if the architecture is real it should
carry a little signal on a sibling symbol, and if it does not, the family is
MGC-specific.

Usage:
  .venv/bin/python research/cycle_20261028/me_port_screen.py <SYMBOL> <tf>
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

DEFAULT_CODE = (ROOT / "research" / "cycle_20261027b" / "strategies" /
            "sweep_cont_market_entry_v2.py")

CODE = DEFAULT_CODE.read_text()
GRID_NAME = "port"

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 12
MIN_PF = 1.25


def grid_cells():
    """Small anchored grid: the validated MGC cell + first-order neighbours."""
    cells = []
    base = {"lookback": 12, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
            "atr_period": 14, "confirm_bars": 6, "ema_period": 0,
            "session_start_utc": -1, "session_end_utc": -1,
            "pd_max_age_days": 4, "sweep_max_atr": 99.0}
    if GRID_NAME == "rb":
        # rubber-band continuation: 4-phase cycle (trend -> box -> sweep ->
        # breakout-with-trend).  Small, mechanised grid.
        for tl, tm, cb, rw, rr in itertools.product(
                (10, 20), (1.0, 1.5, 2.0), (5, 8, 12), (2.0, 3.0),
                (1.0, 1.5, 2.0)):
            cells.append({"atr_period": 14, "trend_lookback": tl,
                          "trend_atr_mult": tm, "consol_bars": cb,
                          "range_max_atr": rw, "sweep_lookback": 8,
                          "sweep_min_atr": 0.05, "break_body_min": 0.55,
                          "rr": rr, "stop_buf_atr": 0.15,
                          "min_risk_atr": 0.20, "max_risk_atr": 4.0,
                          "direction": 0, "trend_mode": 0})
        return cells
    if GRID_NAME == "clgr":
        for rr, bl, md in itertools.product((1.0, 1.5, 2.0), (0.0, 0.35),
                                            (0, 1)):
            cells.append({"atr_period": 14, "rr": rr, "body_min": bl,
                          "max_risk_atr": 4.0, "stop_buf_atr": 0.15,
                          "min_risk_atr": 0.15, "require_not_break": md,
                          "poi_lookback": 0, "poi_min_atr": 0.0})
        return cells
    for setup, ms, dk, rr, mode in itertools.product(
            ("sweep_cont", "pd_sweep_cont"),
            (0.15, 0.30), (0.8, 1.0), (1.0, 1.5, 2.0), ("through",)):
        c = dict(base)
        c.update({"setup": setup, "min_sweep_atr": ms, "disp_k": dk,
                  "rr": rr, "disp_mode": mode})
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
    # directional split of the live book (beta check, finding 9153)
    lt, sh = [], []
    for seg, trades in zip(segs, per["live"]):
        for t in trades:
            (lt if t["direction"] == "long" else sh).append(t)
    res["live_long"] = metrics(lt)
    res["live_short"] = metrics(sh)
    return res


def _tag(p):
    drop = {"atr_period", "confirm_bars", "session_start_utc", "session_end_utc",
            "ema_period", "pd_max_age_days", "sweep_max_atr"}
    return json.dumps({k: v for k, v in p.items() if k not in drop},
                      sort_keys=True)


def main() -> int:
    global CODE, GRID_NAME
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    if len(sys.argv) > 3 and sys.argv[3]:
        CODE = Path(sys.argv[3]).read_text()
    if len(sys.argv) > 4 and sys.argv[4]:
        GRID_NAME = sys.argv[4]
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
        for r in ex.map(_job, [(symbol, tf, c) for c in cells], chunksize=1):
            if r:
                rows.append(r)

    def score(r):
        il, ol, fl = r["live"]["is"], r["live"]["oos"], r["live"]["full"]
        return (min(il["pf"], ol["pf"]) if il["n"] >= MIN_TRADES_IS
                and ol["n"] >= MIN_TRADES_OOS else 0.0)

    rows.sort(key=lambda r: -score(r))

    survivors = []
    for r in rows:
        il, ol, fl = r["live"]["is"], r["live"]["oos"], r["live"]["full"]
        if (il["pf"] >= MIN_PF and ol["pf"] >= MIN_PF
                and il["n"] >= MIN_TRADES_IS and ol["n"] >= MIN_TRADES_OOS
                and fl["n"] >= 20):
            survivors.append(r)

    print(f"\n=== TOP 10 cells (LIVE book) ===")
    for r in rows[:10]:
        print(f"  {_tag(r['params'])}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      base full {r['base']['full']['pf']}  fair full {r['fair']['full']['pf']}")

    print(f"\n=== {len(survivors)} survivors "
          f"(live IS/OOS PF>={MIN_PF}, IS n>={MIN_TRADES_IS}, "
          f"OOS n>={MIN_TRADES_OOS}, full n>=20) ===")
    for r in survivors:
        print(f"  {_tag(r['params'])}")
        print(f"      IS  {json.dumps(r['live']['is'])}")
        print(f"      OOS {json.dumps(r['live']['oos'])}")
        print(f"      full {json.dumps(r['live']['full'])}")
        print(f"      long {json.dumps(r['live_long'])}")
        print(f"      short {json.dumps(r['live_short'])}")
        print(f"      base full {r['base']['full']['pf']}  fair full {r['fair']['full']['pf']}")

    out = ROOT / "outputs" / f"me_port_{symbol}_{tf}_{GRID_NAME}_20261028.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)), "boundary": str(boundary),
         "n_cells": len(cells), "survivors": survivors, "all": rows},
        indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
