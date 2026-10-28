"""Parameter grid for the sweep-MSS-FVG strategy (2026-10-28 cycle).

Screens the structural knobs of the new model on the deep bar archive with
the SAME engine the promotion tool uses (fill at the named price, cost model
v1_realistic_micros, one trade at a time).  IS = the archive minus its last
`oos_days`; OOS = that tail, which is never used for selection.

usage: .venv/bin/python research/cycle_20261028/grid.py <symbol> <tf> <out.json>
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CODE = (ROOT / "research/cycle_20261028/sweep_mss_fvg_liq_target_v1.py").read_text()
PER_DAY = {"5m": 288, "15m": 96, "1h": 24}

BASE = {
    "sweep_lookback": 20, "min_sweep_atr": 0.25, "sweep_age": 12,
    "disp_k": 0.8, "body_ratio_min": 0.5, "fvg_min_atr": 0.10,
    "fvg_expiry": 8, "stop_buf_atr": 0.10, "stop_mode": "fvg",
    "close_in_zone": 1, "atr_period": 14, "max_risk_atr": 2.0,
    "liq_lookback": 60, "min_rr": 1.0, "target_mode": "liquidity",
    "rr": 2.0, "bias_ema": 0,
}


def cells():
    out = []
    for stop_mode in ("fvg", "sweep"):
        for close_in_zone in (1, 0):
            for max_risk in (1.5, 3.0):
                for target_mode in ("liquidity", "rr"):
                    for lb in (12, 20):
                        for ms in (0.20, 0.35):
                            for dk in (0.7, 1.0):
                                c = dict(BASE)
                                c.update(stop_mode=stop_mode, close_in_zone=close_in_zone,
                                         max_risk_atr=max_risk, target_mode=target_mode,
                                         sweep_lookback=lb, min_sweep_atr=ms, disp_k=dk)
                                out.append(c)
    return out


def _bars(symbol, tf):
    df = pd.read_parquet(ROOT / f"data/bars_archive/{symbol}_{tf}.parquet")
    return df.sort_values("ts").reset_index(drop=True)


def _split(df, tf, oos_days, is_days):
    n = len(df)
    per = PER_DAY[tf]
    oos_bars = per * oos_days
    oos = df.iloc[n - oos_bars:].reset_index(drop=True)
    is_df = df.iloc[: n - oos_bars].reset_index(drop=True)
    cap = per * is_days
    if len(is_df) > cap:
        is_df = is_df.tail(cap).reset_index(drop=True)
    return is_df, oos


def _one(job):
    symbol, tf, split, cell = job
    from src.research import custom_strategy_runtime as csr
    df = _bars(symbol, tf)
    is_df, oos = _split(df, tf, oos_days=30, is_days=240)
    bars = is_df if split == "is" else oos
    m = csr.run_custom_backtest_on_bars(code=CODE, bars=bars, params=cell,
                                         symbol=symbol, qty=1, timeout_s=900)
    m["_first"] = str(bars["ts"].iloc[0])
    m["_last"] = str(bars["ts"].iloc[-1])
    m["_bars"] = int(len(bars))
    return symbol, tf, split, cell, m


def main():
    symbol, tf = sys.argv[1], sys.argv[2]
    out_path = Path(sys.argv[3])
    cs = cells()
    jobs = [(symbol, tf, "is", c) for c in cs]
    rows = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=8) as ex:
        for symbol_, tf_, split, cell, m in ex.map(_one, jobs):
            rows.append({"cell": cell, "metrics": m})
            print(f"stop={cell['stop_mode']} zone={cell['close_in_zone']} "
                  f"mrisk={cell['max_risk_atr']} tgt={cell['target_mode']} "
                  f"lb={cell['sweep_lookback']} ms={cell['min_sweep_atr']} dk={cell['disp_k']} "
                  f"| PF={m.get('pf'):.2f} n={m.get('n_trades')} "
                  f"${m.get('total_pnl')} E[R]={m.get('expectancy_r')} "
                  f"err={m.get('error')}", flush=True)
    out_path.write_text(json.dumps(rows, indent=2, default=str))
    print(f"wrote {out_path} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
