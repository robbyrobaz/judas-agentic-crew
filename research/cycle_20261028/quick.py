"""Quick single-cell timing / sanity run for the new sweep-MSS-FVG strategy.

Uses the SAME engine the tool uses (src.research.custom_strategy_runtime
.run_custom_backtest_on_bars) so the fill model, the cost model
(v1_realistic_micros) and the geometry are identical -- only the bar source is
local (data/bars_archive) instead of the IBKR union, which saves a round trip.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CODE = (ROOT / "research/cycle_20261028/sweep_mss_fvg_liq_target_v1.py").read_text()
PER_DAY = {"5m": 288, "15m": 96, "1h": 24}


def load(symbol: str, tf: str) -> pd.DataFrame:
    df = pd.read_parquet(ROOT / f"data/bars_archive/{symbol}_{tf}.parquet")
    return df.sort_values("ts").reset_index(drop=True)


def window(df: pd.DataFrame, tf: str, days: int) -> pd.DataFrame:
    per = PER_DAY[tf]
    if days and len(df) > per * days:
        df = df.tail(per * days).reset_index(drop=True)
    return df


def run(symbol: str, tf: str, days: int, params: dict | None = None):
    from src.research import custom_strategy_runtime as csr
    bars = window(load(symbol, tf), tf, days)
    t0 = time.time()
    m = csr.run_custom_backtest_on_bars(code=CODE, bars=bars, params=params or {},
                                         symbol=symbol, qty=1, timeout_s=300)
    m["_secs"] = round(time.time() - t0, 1)
    m["_bars"] = len(bars)
    m["_first"] = str(bars["ts"].iloc[0])
    m["_last"] = str(bars["ts"].iloc[-1])
    return m


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 90
    params = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    m = run(symbol, tf, days, params)
    keep = {k: m.get(k) for k in ("n_trades", "n_wins", "n_losses", "pf", "total_pnl",
                                  "expectancy_r", "max_drawdown", "cost_model", "error",
                                  "stale_fill_n", "backtest_fill_valid", "bars_span_days",
                                  "_secs", "_bars", "_first", "_last")}
    print(json.dumps(keep, indent=2, default=str))
