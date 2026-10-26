"""CHRONOLOGICAL SEGMENT CHECK for the market-entry architecture (2026-10-26).

The 2026-10-25 sweep picked its winners as the MAX over a grid scored on one
window, so the headline PF is selection-biased.  Two things make a cell
promotable:

  1. STABILITY across chronological segments of the archive -- if the edge is
     real it should not live in one lucky month;
  2. PLATEAU -- the NEIGHBOURING parameter cells should also clear PF 1.0,
     otherwise the "winner" is a single noise spike.

The LIVE book is used throughout: entry = NEXT BAR OPEN (production market
order), brackets re-anchored to the fill, gap-aware exits,
cost_model=v1_realistic_micros, qty=1.  This is the only fill the broker can
give us (finding #9134: there is no LIMIT entry path in this repo).

Usage:
  .venv/bin/python research/cycle_20261026/segment_check.py <symbol> <tf> <json-configs>
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import load_bars, metrics, run_books  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261025" / "strategies" /
        "market_entry_ict_v1.py").read_text()

N_SEG = 4


def _job(args):
    symbol, tf, params, seg_idx = args
    bars = load_bars(symbol, tf, days=0)
    if bars is None or len(bars) < 400:
        return None
    n = len(bars)
    lo = int(n * seg_idx / N_SEG)
    hi = int(n * (seg_idx + 1) / N_SEG)
    sl = bars.iloc[lo:hi].reset_index(drop=True)
    if len(sl) < 300:
        return None
    b, lv, fa, meta = run_books(CODE, sl, params, symbol)
    return {"symbol": symbol, "tf": tf, "params": params, "seg": seg_idx,
            "first": str(sl["ts"].iloc[0]), "last": str(sl["ts"].iloc[-1]),
            "live": metrics(lv), "fair": metrics(fa), "base": metrics(b),
            "meta": meta}


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    configs = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []
    jobs = [(symbol, tf, cfg, i) for cfg in configs for i in range(N_SEG)]
    rows = []
    with ProcessPoolExecutor(max_workers=12) as ex:
        for r in ex.map(_job, jobs):
            if r:
                rows.append(r)

    print(f"=== {symbol} {tf}: {len(configs)} configs x {N_SEG} chronological segments (LIVE book) ===")
    for cfg in configs:
        rs = [r for r in rows if r["params"] == cfg]
        rs.sort(key=lambda r: r["seg"])
        parts = []
        for r in rs:
            lv = r["live"]
            parts.append(f"S{r['seg']+1} n={lv['n']:4d} pf={lv['pf']:5.2f} "
                         f"E[R]={lv['e_r']:+.2f} ${lv['total_usd']:+8.0f}")
        all_n = sum(r["live"]["n"] for r in rs)
        pos = sum(1 for r in rs if r["live"]["pf"] > 1.0)
        tag = json.dumps({k: v for k, v in cfg.items() if k != "setup"})
        print(f"  {cfg.get('setup',''):14s} {tag}")
        print(f"      {' | '.join(parts)}   [segments PF>1: {pos}/{len(rs)}, total n={all_n}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
