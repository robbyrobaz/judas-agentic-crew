"""GENERIC honest-fill sweep for a market-entry architecture.

Runs one code file across (symbol, timeframe) x param grid under the
BASE / FAIR / LIVE books of research/cycle_20261025/honest_screen.py.

THE VERDICT COLUMN IS `live` (production semantics: market at the next bar's
open, brackets re-anchored, gap-aware exits).  `base` is what the house
backtester reports -- for a market-entry code base ~= fair, and any base/live
divergence is a geometry bug, not an edge.

Usage:  .venv/bin/python research/cycle_20261025/sweep_arch.py <code.py> <out.json>
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import screen  # noqa: E402

PAIRS = [("MGC", "15m"), ("MNQ", "15m"), ("ZF", "15m"), ("6J", "15m"), ("MCL", "15m"),
         ("MGC", "5m"), ("MNQ", "5m"), ("ZF", "5m"), ("6J", "5m"), ("MCL", "5m")]

CODE_PATH = None
CODE = ""


def grid():
    g = []
    for lookback in (12, 24):
        for min_sweep_atr in (0.15, 0.30):
            for disp_k in (0.6, 0.9):
                for rr in (1.5, 2.0, 3.0):
                    g.append(dict(lookback=lookback, min_sweep_atr=min_sweep_atr,
                                  disp_k=disp_k, rr=rr, body_ratio_min=0.5,
                                  max_age=24, atr_period=14, stop_buf_atr=0.15,
                                  confirm="prev_high"))
    return g


def _job(args):
    symbol, tf, params, days = args
    try:
        r = screen(CODE, symbol, tf, params, days=days)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "tf": tf, "params": params,
                "error": f"{type(exc).__name__}: {exc}"}
    r["params"] = params
    return r


def main() -> int:
    global CODE, CODE_PATH
    CODE_PATH = Path(sys.argv[1])
    CODE = CODE_PATH.read_text()
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else \
        ROOT / "outputs" / f"sweep_{CODE_PATH.stem}.json"

    jobs = []
    for symbol, tf in PAIRS:
        days = 120 if tf == "15m" else 45
        for params in grid():
            jobs.append((symbol, tf, params, days))

    t0 = time.time()
    rows = []
    jsonl = out.with_suffix(".jsonl")
    out.parent.mkdir(exist_ok=True)
    with jsonl.open("w") as fh:
        with ProcessPoolExecutor(max_workers=12) as ex:
            for r in ex.map(_job, jobs):
                rows.append(r)
                fh.write(json.dumps(r, default=str) + "\n")
                fh.flush()
                lv = r.get("live") or {}
                if lv.get("n"):
                    print(f"{r['symbol']:4s} {r['tf']:3s} n={lv['n']:4d} "
                          f"LIVE pf={lv['pf']:5.2f} E[R]={lv['e_r']:6.3f} "
                          f"WR={lv['wr_pct']:5.1f} ${lv['total_usd']:9.0f} "
                          f"dd=${lv['max_dd']:8.0f} | {json.dumps({k: v for k, v in r['params'].items() if k not in ('atr_period', 'stop_buf_atr', 'confirm', 'body_ratio_min', 'max_age')})}",
                          flush=True)
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {out} ({len(rows)} runs, {time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
