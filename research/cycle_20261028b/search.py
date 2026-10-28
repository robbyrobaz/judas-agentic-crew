"""CLEAN random search for the v2 sweep-MSS-FVG family (2026-10-28b cycle).

WHY
---
The 2026-10-28 screen (research/cycle_20261028/screen.py) searched 400 random
cells of this space on MGC 15m and reported cells with PF up to 5.5 -- but
that screen replayed the RAW archive, which is padded with PLACEHOLDER bars
(35% of MGC 15m bars have zero range; 71% for 6J 15m).  Placeholders
manufacture fake sweeps and fake displacement legs, so those numbers are
artefacts.  This script repeats the search on the v2 strategy, which only
trades inside a CONTIGUOUS RUN OF REAL BARS.

OUTPUT
------
One JSON per (symbol, tf): every cell with its IS / OOS / ALL aggregates.

usage:
  .venv/bin/python research/cycle_20261028b/search.py <SYMBOL> <tf> <n_cells> <out.json> [seed]
"""
from __future__ import annotations

import json
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261028b"))

import numpy as np  # noqa: E402

import replay_v2 as R  # noqa: E402

SPACE = {
    "sweep_lookback": [8, 12, 16, 24],
    "min_sweep_atr": [0.05, 0.10, 0.20, 0.35],
    "sweep_age": [6, 10, 16, 24],
    "pivot_lookback": [3, 5, 8, 12],
    "disp_k": [0.4, 0.6, 0.8, 1.0],
    "body_ratio_min": [0.3, 0.4, 0.5, 0.6],
    "fvg_min_atr": [0.0, 0.05, 0.10, 0.20],
    "fvg_expiry": [4, 6, 10, 16],
    "stop_buf_atr": [0.0, 0.10, 0.25],
    "stop_mode": ["fvg", "sweep"],
    "close_in_zone": [0, 1],
    "max_risk_atr": [1.5, 3.0, 5.0],
    "liq_lookback": [30, 60, 120],
    "min_rr": [0.5, 1.0, 1.5, 2.0],
    "target_mode": ["liquidity", "rr"],
    "rr": [1.0, 1.5, 2.0, 3.0],
    "bias_ema": [0, 50, 100],
    "mss_required": [1],
    "min_run_bars": [60],
}

OOS_DAYS = 30


def sample_cell(rng):
    return {k: rng.choice(vals) for k, vals in SPACE.items()}


def clean_boundary(df):
    """Split point INSIDE the longest contiguous run of real bars (60/40).

    The archive's clean coverage is only the last few months, so a
    calendar split would put nearly every trade in one bucket.  The split is
    taken inside the LONGEST real-bar run, which is where the strategy can
    actually trade.
    """
    real = ((df["high"] - df["low"]) > 0).to_numpy() & (df["volume"] > 0).to_numpy()
    best = (0, 0)
    start = 0
    for i in range(len(real) + 1):
        if i == len(real) or not real[i]:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = i + 1
    a, b = best
    if b - a < 50:
        return df["ts"].iloc[max(0, len(df) - R.PER_DAY.get(df.attrs.get("tf", "15m"), 96) * OOS_DAYS)]
    cut = a + int(0.6 * (b - a))
    return df["ts"].iloc[cut]


def _one(job):
    symbol, tf, cell = job
    mod = R.load_strategy()
    df = R.load_bars(symbol, tf)
    boundary = clean_boundary(df)
    tr = R.replay(df, cell, symbol, mod)
    agg = R.split_agg(tr, boundary)
    return symbol, tf, cell, agg


def main():
    symbol, tf = sys.argv[1], sys.argv[2]
    n_cells = int(sys.argv[3])
    out_path = Path(sys.argv[4])
    seed = int(sys.argv[5]) if len(sys.argv) > 5 else 20261028
    rng = random.Random(seed)
    seen = set()
    cells = []
    while len(cells) < n_cells:
        c = sample_cell(rng)
        key = json.dumps(c, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        cells.append(c)

    t0 = time.time()
    jobs = [(symbol, tf, c) for c in cells]
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for symbol, tf, cell, agg in ex.map(_one, jobs, chunksize=2):
            rows.append({"cell": cell, **agg})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=1))
    print(f"{symbol} {tf}: {len(rows)} cells in {time.time() - t0:.1f}s -> {out_path}")
    live = [r for r in rows if r["all"]["n"] >= 8]
    live.sort(key=lambda r: -r["all"]["pf"])
    for r in live[:10]:
        print(f"  all pf={r['all']['pf']} n={r['all']['n']} e_r={r['all']['e_r']} "
              f"is pf={r['is']['pf']}/{r['is']['n']} oos pf={r['oos']['pf']}/{r['oos']['n']}")


if __name__ == "__main__":
    main()
