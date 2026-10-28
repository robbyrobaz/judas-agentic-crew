"""5m SWEEP->DISPLACEMENT grid on the `live` (executable) fill book, with a
TIME-SPLIT IS/OOS protocol.  (2026-10-28 cycle)

WHY 5m
------
The market-entry sweep->displacement architecture (csid=439 family) is the ONLY
family in this book that survived the honest-fill audit (finding #9146) and a
TRUE out-of-sample holdout (candidate #7382).  It is PROMOTED on MGC 15m
(live PF 1.722, n=71, finding 9153).  Its cross-symbol 15m ports are dead:
ZF live PF 0.88 (n=18), 6J live PF 0.968 (n=33) -- measured this cycle with
the hygiene code under the same live fill.

The MGC 5m version of the SAME architecture has NEVER been tested, and MGC 5m
is the deepest clean dataset in the book (22,563 clean bars, one 17,759-bar
clean run = 2026-06-22 -> 2026-10-23, 92% clean).  5m also fires far more
often than 15m, so a candidate that works there reaches a meaningful live
sample in days rather than months.

PROTOCOL
--------
* FILLS: `live` book -- MARKET at the OPEN of the bar AFTER the decision bar,
  brackets re-anchored, gap-aware exits (production semantics, #9134),
  cost_model=v1_realistic_micros, qty=1.  The `base` book (house engine:
  fill at the price the strategy NAMES) is printed alongside only to show the
  retro-fill gap; it is never used for promotion.
* DATA: per CLEAN SEGMENT (placeholder rows dropped) so a trade can never
  jump a time gap; trades from all segments are aggregated.
* IS/OOS: TIME split over the global clean span, first 60% = IS, last 40% =
  OOS.  Cells are RANKED ON IS ONLY; the OOS book is the admission test
  (n>=20 OOS trades and OOS live PF > 1.15) -- so the OOS window is never
  used to pick the cell.

Usage:
  .venv/bin/python research/cycle_20261028/sweep5m_grid.py <SYMBOL> <tf> <grid.json>
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments  # noqa: E402

HYGIENE = (ROOT / "research" / "cycle_20261027" / "strategies" /
           "market_entry_sweep_cont_hygiene_v1.py").read_text()

IS_FRAC = 0.60
MIN_SEG = 300
MIN_TRADES_OOS = 20


def grid_cells():
    cells = []
    for lb, ms, dk, rr in itertools.product(
            (8, 12, 20), (0.30, 0.45, 0.60), (0.8, 1.0, 1.2), (1.0, 1.5, 2.0)):
        cells.append({"lookback": lb, "min_sweep_atr": ms, "disp_k": dk,
                      "rr": rr, "confirm_bars": 6, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    return cells


def _one(job):
    symbol, tf, cell = job
    segs = clean_segments(symbol, tf, MIN_SEG)
    live, base = [], []
    t0 = min(s["ts"].iloc[0] for s in segs)
    t1 = max(s["ts"].iloc[-1] for s in segs)
    split = t0 + (t1 - t0) * IS_FRAC
    timed_out = False
    for seg in segs:
        b_t, l_t, _f_t, meta = run_books(HYGIENE, seg, cell, symbol, budget_s=900.0)
        if meta.get("load_error"):
            return cell, {"error": meta.get("error")}
        if meta.get("timed_out"):
            timed_out = True
        # run_books reports entry_bar (index INSIDE the segment); stamp the
        # timestamp so the IS/OOS partition is by TIME, not by bar index.
        seg_ts = seg["ts"].reset_index(drop=True)
        for book in (b_t, l_t):
            for t in book:
                try:
                    t["entry_ts"] = seg_ts.iloc[int(t["entry_bar"])]
                except Exception:  # noqa: BLE001
                    t["entry_ts"] = t0
        base += b_t
        live += l_t
    out = {"cell": cell, "timed_out": timed_out, "split": str(split)}
    for name, book in (("live", live), ("base", base)):
        is_t = [t for t in book if t["entry_ts"] < split] if book else []
        oos_t = [t for t in book if t["entry_ts"] >= split] if book else []
        out[name] = {"is": metrics(is_t), "oos": metrics(oos_t), "all": metrics(book)}
    return cell, out


def _entry_ts_patch():
    """run_books records entry_bar but not a timestamp; add one via the caller."""
    return None


def main():
    symbol = sys.argv[1].upper()
    tf = sys.argv[2] if len(sys.argv) > 2 else "5m"
    segs = clean_segments(symbol, tf, MIN_SEG)
    if not segs:
        print(f"{symbol} {tf}: no clean segment >= {MIN_SEG} bars")
        return
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, "
          f"{segs[0]['ts'].iloc[0]} -> {segs[-1]['ts'].iloc[-1]}", flush=True)
    cells = grid_cells()
    jobs = [(symbol, tf, c) for c in cells]
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for cell, res in ex.map(_one, jobs):
            if "error" in res:
                print(cell, res, flush=True)
                continue
            li, lo = res["live"]["is"], res["live"]["oos"]
            rows.append(res)
            print(f"lb={cell['lookback']:>2} ms={cell['min_sweep_atr']:.2f} "
                  f"dk={cell['disp_k']:.1f} rr={cell['rr']:.1f} | "
                  f"IS n={li['n']:>3} pf={li['pf']:>6} | "
                  f"OOS n={lo['n']:>3} pf={lo['pf']:>6} e_r={lo['e_r']:>6} "
                  f"pnl={lo['total_usd']:>9,.0f} | "
                  f"ALL n={res['live']['all']['n']:>3} pf={res['live']['all']['pf']}",
                  flush=True)
    out = ROOT / "outputs" / f"sweep5m_grid_{symbol}_{tf}_20261028.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
