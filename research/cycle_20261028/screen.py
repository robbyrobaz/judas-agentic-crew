"""Random search over the sweep-MSS-FVG parameter space (2026-10-28).

Stage 1: sample N cells, replay each on the IS window of one symbol/timeframe
with the SAME fill + cost model as the promotion engine, and report the ones
that clear a real margin (n>=25 trades, E[R] > 0.05).

usage: .venv/bin/python research/cycle_20261028/screen.py <symbol> <tf> <n_cells> <out.json>
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
sys.path.insert(0, str(ROOT / "research/cycle_20261028"))

import replay as R  # noqa: E402

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
    "atr_period": [14],
    "max_risk_atr": [1.0, 1.5, 2.0, 3.0, 5.0],
    "liq_lookback": [30, 60, 120],
    "min_rr": [0.5, 0.8, 1.0, 1.5, 2.0],
    "target_mode": ["liquidity", "rr"],
    "rr": [1.0, 1.5, 2.0, 3.0],
    "bias_ema": [0, 0, 0, 50, 100],
}


def sample_cell(rng):
    cell = {}
    for k, vals in SPACE.items():
        cell[k] = rng.choice(vals)
    return cell


def _one(job):
    symbol, tf, split, cell = job
    mod = R.load_strategy()
    is_df, oos = R.load_bars(symbol, tf)
    bars = is_df if split == "is" else oos
    tr = R.replay(bars, cell, symbol, mod)
    a = R.agg(tr)
    a["_first"] = str(bars["ts"].iloc[0])
    a["_last"] = str(bars["ts"].iloc[-1])
    return symbol, tf, split, cell, a


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
    jobs = [(symbol, tf, "is", c) for c in cells]
    rows = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=8) as ex:
        for symbol_, tf_, split, cell, a in ex.map(_one, jobs):
            rows.append({"cell": cell, "agg": a})
    rows.sort(key=lambda r: (-(r["agg"]["e_r"]), -r["agg"]["n"]))
    for r in rows[:25]:
        a = r["agg"]
        c = r["cell"]
        print(f"n={a['n']:3d} PF={a['pf']:5.2f} E[R]={a['e_r']:+.3f} ${a['pnl']:>9.2f} "
              f"wr={a['wr']:4.1f} | stop={c['stop_mode']} zone={c['close_in_zone']} "
              f"lb={c['sweep_lookback']} ms={c['min_sweep_atr']} age={c['sweep_age']} "
              f"piv={c['pivot_lookback']} dk={c['disp_k']} br={c['body_ratio_min']} "
              f"fvg={c['fvg_min_atr']}/{c['fvg_expiry']} mrisk={c['max_risk_atr']} "
              f"liq={c['liq_lookback']} rr={c['min_rr']}/{c['target_mode']}/{c['rr']} "
              f"bias={c['bias_ema']}")
    out_path.write_text(json.dumps(rows, indent=2, default=str))
    print(f"wrote {out_path}  ({len(rows)} cells, {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
