"""PARAM SWEEP over the honest market-entry architectures (2026-10-25 cycle).

Runs research/cycle_20261025/strategies/market_entry_ict_v1.py across
(symbol, timeframe) x param grid and reports BASE (house backtester fill),
FAIR (decision close) and LIVE (next bar open, production) books side by side.

Usage:  .venv/bin/python research/cycle_20261025/sweep_honest.py [out.json]
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

CODE = (ROOT / "research" / "cycle_20261025" / "strategies" /
        "market_entry_ict_v1.py").read_text()

PAIRS = [("MGC", "15m"), ("MNQ", "15m"), ("ZF", "15m"),
         ("6J", "15m"), ("MCL", "15m"),
         ("MGC", "5m"), ("MNQ", "5m"), ("ZF", "5m"),
         ("6J", "5m"), ("MCL", "5m")]

GRID = []


def _add(setup, **kw):
    GRID.append(dict(setup=setup, **kw))


# --- sweep_rev: fade a failed breakout of a rolling level ------------------
for lookback in (12, 20, 32, 48):
    for min_sweep_atr in (0.10, 0.20, 0.35):
        for rr in (1.5, 2.0):
            _add("sweep_rev", lookback=lookback, min_sweep_atr=min_sweep_atr,
                 rr=rr, stop_buf_atr=0.15, atr_period=14)

# --- sweep_cont: sweep -> displacement back through the level -------------
for lookback in (12, 20, 32):
    for min_sweep_atr in (0.15, 0.30):
        for disp_k in (0.7, 1.0):
            for rr in (1.5, 2.0):
                _add("sweep_cont", lookback=lookback, min_sweep_atr=min_sweep_atr,
                     disp_k=disp_k, rr=rr, confirm_bars=6, body_ratio_min=0.55,
                     stop_buf_atr=0.15, atr_period=14)

# --- disp_break: displacement beyond a rolling level -----------------------
for lookback in (12, 20, 32):
    for disp_k in (0.8, 1.1):
        for stop_atr in (0.6, 1.0):
            for rr in (1.5, 2.0):
                _add("disp_break", lookback=lookback, disp_k=disp_k,
                     stop_atr=stop_atr, rr=rr, body_ratio_min=0.6,
                     atr_period=14)

# --- fvg_touch: iFVG reversion, market out on the touch ------------------
for fvg_lookback in (8, 16, 24):
    for rr in (1.5, 2.0, 3.0):
        for buf in (0.10, 0.25):
            _add("fvg_touch", fvg_lookback=fvg_lookback, rr=rr,
                 stop_buf_atr=buf, atr_period=14)

# --- disp_pullback: displacement -> pullback that holds --------------------
for pullback_bars in (3, 5):
    for retrace in (0.4, 0.6):
        for disp_k in (0.8, 1.1):
            for rr in (1.5, 2.0):
                _add("disp_pullback", pullback_bars=pullback_bars,
                     retrace=retrace, disp_k=disp_k, rr=rr,
                     body_ratio_min=0.55, stop_buf_atr=0.15, atr_period=14)


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
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "outputs" / "honest_sweep_20261025.json"
    jobs = []
    for symbol, tf in PAIRS:
        days = 120 if tf == "15m" else 45
        for params in GRID:
            jobs.append((symbol, tf, params, days))
    t0 = time.time()
    rows = []
    jsonl = out.with_suffix(".jsonl")
    with jsonl.open("w") as fh:
        with ProcessPoolExecutor(max_workers=12) as ex:
            for r in ex.map(_job, jobs):
                rows.append(r)
                fh.write(json.dumps(r, default=str) + "\n")
                fh.flush()
                if r.get("live") and r["live"].get("n"):
                    lv = r["live"]
                    print(f"{r['symbol']:4s} {r['tf']:3s} {r['params']['setup']:14s} "
                          f"{json.dumps({k: v for k, v in r['params'].items() if k != 'setup'})} "
                          f"| base n={r['base']['n']:4d} pf={r['base']['pf']:6} "
                          f"| live n={lv['n']:4d} pf={lv['pf']:6} E[R]={lv['e_r']:6} "
                          f"WR={lv['wr_pct']:5} ${lv['total_usd']:9.0f}", flush=True)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {out} ({len(rows)} runs, {time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
