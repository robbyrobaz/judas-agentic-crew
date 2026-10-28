"""Generic live-book grid runner (2026-10-28 cycle).

Runs a set of parameter cells for ONE (code, symbol, timeframe) through the
HONEST `live` book (market at the open of the bar after the decision bar,
brackets re-anchored, gap-aware exits, cost_model=v1_realistic_micros,
qty=1) over the CLEAN SEGMENTS of the archive, and splits the trades by TIME
into IS (first 60% of the global clean span) and OOS (last 40%).

Cells are RANKED ON IS ONLY; OOS is the admission test.

Usage:
  .venv/bin/python research/cycle_20261028/grid_run.py <code.py> <SYMBOL> <tf> <cells.json>
"""
from __future__ import annotations

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

IS_FRAC = 0.60
MIN_SEG = 300

_CODE_PATH = None
_CODE = None


def _init(code_path):
    global _CODE_PATH, _CODE
    _CODE_PATH = code_path
    _CODE = Path(code_path).read_text()


def _one(job):
    symbol, tf, cell = job
    segs = clean_segments(symbol, tf, MIN_SEG)
    if not segs:
        return cell, {"error": "no clean segment"}
    live, base = [], []
    t0 = min(s["ts"].iloc[0] for s in segs)
    t1 = max(s["ts"].iloc[-1] for s in segs)
    split = t0 + (t1 - t0) * IS_FRAC
    for seg in segs:
        b_t, l_t, _f_t, meta = run_books(_CODE, seg, cell, symbol, budget_s=900.0)
        if meta.get("load_error"):
            return cell, {"error": meta.get("error")}
        seg_ts = seg["ts"].reset_index(drop=True)
        for book in (b_t, l_t):
            for t in book:
                try:
                    t["entry_ts"] = seg_ts.iloc[int(t["entry_bar"])]
                except Exception:  # noqa: BLE001
                    t["entry_ts"] = t0
        base += b_t
        live += l_t
    out = {"cell": cell, "split": str(split)}
    for name, book in (("live", live), ("base", base)):
        is_t = [t for t in book if t["entry_ts"] < split]
        oos_t = [t for t in book if t["entry_ts"] >= split]
        out[name] = {"is": metrics(is_t), "oos": metrics(oos_t), "all": metrics(book)}
    return cell, out


def main():
    code_path, symbol, tf, cells_path = sys.argv[1], sys.argv[2].upper(), sys.argv[3], sys.argv[4]
    cells = json.loads(Path(cells_path).read_text())
    segs = clean_segments(symbol, tf, MIN_SEG)
    if not segs:
        print(f"{symbol} {tf}: no clean segment >= {MIN_SEG} bars")
        return
    print(f"{symbol} {tf}: {len(segs)} clean segments, {sum(len(s) for s in segs)} bars, "
          f"{segs[0]['ts'].iloc[0]} -> {segs[-1]['ts'].iloc[-1]} | {len(cells)} cells",
          flush=True)
    jobs = [(symbol, tf, c) for c in cells]
    rows = []
    with ProcessPoolExecutor(max_workers=8, initializer=_init, initargs=(code_path,)) as ex:
        for cell, res in ex.map(_one, jobs):
            if "error" in res:
                print(cell, res, flush=True)
                continue
            rows.append(res)
            li, lo, al = res["live"]["is"], res["live"]["oos"], res["live"]["all"]
            bb = res["base"]["all"]
            print(f"{json.dumps(cell, sort_keys=True)}\n   IS n={li['n']:>3} pf={li['pf']:>6} | "
                  f"OOS n={lo['n']:>3} pf={lo['pf']:>6} e_r={lo['e_r']:>6} pnl={lo['total_usd']:>9,.0f} | "
                  f"ALL n={al['n']:>3} pf={al['pf']:>6} | base(all) n={bb['n']:>3} pf={bb['pf']:>6}",
                  flush=True)
    out = ROOT / "outputs" / f"grid_{Path(code_path).stem}_{symbol}_{tf}_20261028.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
