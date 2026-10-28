"""Generic honest-fill grid runner for a single strategy file (2026-10-28).

Usage:
  .venv/bin/python research/cycle_20261028/zen_grid.py <SYMBOL> <tf> <strategy_file.py> <cells.json>

Same protocol as the MGC sweep grid:
  * `live` book = PRODUCTION fill (market at the OPEN of the bar AFTER the
    decision bar, brackets re-anchored, gap-aware exits),
    cost_model=v1_realistic_micros, qty=1.
  * per CLEAN SEGMENT (placeholder rows dropped) so a trade can never jump a
    time gap; trades from all segments are aggregated.
  * TIME split over the global clean span: first 60% = IS, last 40% = OOS.
    Cells are RANKED ON IS ONLY; OOS is the admission test
    (n_oos >= 20 and OOS live PF > 1.15).
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

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments  # noqa: E402

IS_FRAC = 0.60
MIN_SEG = 300


def _one(job):
    symbol, tf, code, cell = job
    segs = clean_segments(symbol, tf, MIN_SEG)
    if not segs:
        return symbol, cell, {"error": "no clean segment >= %d bars" % MIN_SEG}
    live, base = [], []
    t0 = min(s["ts"].iloc[0] for s in segs)
    t1 = max(s["ts"].iloc[-1] for s in segs)
    split = t0 + (t1 - t0) * IS_FRAC
    timed_out = False
    for seg in segs:
        b_t, l_t, _f_t, meta = run_books(code, seg, cell, symbol, budget_s=900.0)
        if meta.get("load_error"):
            return symbol, cell, {"error": meta.get("error")}
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
    out = {"split": str(split), "timed_out": timed_out, "n_segments": len(segs),
           "n_bars": int(sum(len(s) for s in segs)),
           "first_bar": str(segs[0]["ts"].iloc[0]),
           "last_bar": str(segs[-1]["ts"].iloc[-1])}
    for name, book in (("live", live), ("base", base)):
        is_t = [t for t in book if t["entry_ts"] < split] if book else []
        oos_t = [t for t in book if t["entry_ts"] >= split] if book else []
        out[name] = {"is": metrics(is_t), "oos": metrics(oos_t), "all": metrics(book)}
    return symbol, cell, out


def main():
    symbol = sys.argv[1].upper()
    tf = sys.argv[2] if len(sys.argv) > 2 else "5m"
    strat = (ROOT / sys.argv[3]).resolve()
    cells = json.loads(Path(sys.argv[4]).read_text())
    code = strat.read_text()
    segs = clean_segments(symbol, tf, MIN_SEG)
    if not segs:
        print(f"{symbol} {tf}: no clean segment >= {MIN_SEG} bars")
        return
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, "
          f"{segs[0]['ts'].iloc[0]} -> {segs[-1]['ts'].iloc[-1]}", flush=True)
    jobs = [(symbol, tf, code, c) for c in cells]
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for _sym, cell, res in ex.map(_one, jobs):
            if "error" in res:
                print(cell, res, flush=True)
                continue
            li, lo, la = res["live"]["is"], res["live"]["oos"], res["live"]["all"]
            rows.append({"symbol": symbol, "tf": tf, "cell": cell, **res})
            print(f"{json.dumps(cell, sort_keys=True)} | "
                  f"IS n={li['n']:>3} pf={li['pf']:>6} | "
                  f"OOS n={lo['n']:>3} pf={lo['pf']:>6} e_r={lo['e_r']:>6} | "
                  f"ALL n={la['n']:>3} pf={la['pf']:>6} usd={la['total_usd']:>9,.0f}",
                  flush=True)
    out = ROOT / "outputs" / f"zengrid_{symbol}_{tf}_20261028.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
