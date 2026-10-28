"""BOUNDARY-EXTENSION grid for the 5m sweep->displacement family (2026-10-28).

WHY
---
Yesterday's grid (outputs/sweep5m_grid_MGC_5m_20261028.json) found its best
cells pinned to the EDGE of every axis:

    lookback      = 20   (axis max was 20)
    min_sweep_atr = 0.6  (axis max was 0.60)
    rr            = 2.0  (axis max was 2.0)

A cell that sits on the boundary of the search space is either (a) the edge of
a real, monotone effect, or (b) a knife-edge that dies the moment you walk
outside the grid.  This file walks OUTSIDE the boundary to tell the two
apart.  If the edge is real, PF holds or improves; if it is a knife-edge, PF
collapses immediately.

Same protocol as yesterday (research/cycle_20261028/sweep5m_grid.py):
  * `live` book = PRODUCTION fill (market at the OPEN of the bar AFTER the
    decision bar, brackets re-anchored, gap-aware exits),
    cost_model=v1_realistic_micros, qty=1.
  * per CLEAN SEGMENT (placeholder rows dropped) so a trade can never jump a
    time gap; trades from all segments are aggregated.
  * TIME split over the global clean span: first 60% = IS, last 40% = OOS.
    Cells are RANKED ON IS ONLY; OOS is the admission test
    (n_oos >= 20 and OOS live PF > 1.15).

Usage:
  .venv/bin/python research/cycle_20261028/sweep5m_grid_ext.py <SYMBOL> <tf>
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

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments  # noqa: E402

HYGIENE = (ROOT / "research" / "cycle_20261028" / "strategies" /
           "sweep_cont_hygiene_5m_v1.py").read_text()

IS_FRAC = 0.60
MIN_SEG = 300


def grid_cells():
    cells = []
    for lb, ms, dk, rr in itertools.product(
            (20, 24, 30), (0.60, 0.75, 0.90), (0.8, 1.0, 1.2), (2.0, 2.5, 3.0)):
        cells.append({"lookback": lb, "min_sweep_atr": ms, "disp_k": dk,
                      "rr": rr, "confirm_bars": 6, "body_ratio_min": 0.55,
                      "stop_buf_atr": 0.15, "atr_period": 14})
    return cells


def _one(job):
    symbol, tf, cell = job
    segs = clean_segments(symbol, tf, MIN_SEG)
    live, base = [], []
    if not segs:
        return cell, {"error": "no clean segments"}
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
    out = ROOT / "outputs" / f"sweep5m_grid_ext_{symbol}_{tf}_20261028.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
